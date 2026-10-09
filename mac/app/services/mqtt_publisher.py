from __future__ import annotations

import json
import time
from datetime import datetime
from threading import Event
from typing import Any

from app.models.config import RunConfig
from app.models.entities import EntityRecord, MqttPublishRecord


def _load_mqtt_client():
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'paho-mqtt'. Install dependencies with: pip install -r requirements.txt"
        ) from exc
    return mqtt


def publish_telemetry(
    config: RunConfig,
    run_id: str,
    devices: list[EntityRecord],
    stop_event: Event | None = None,
) -> list[MqttPublishRecord]:
    records: list[MqttPublishRecord] = []
    try:
        mqtt = _load_mqtt_client()
    except RuntimeError as exc:
        for device in devices:
            for seq in range(1, config.telemetry_per_device + 1):
                if stop_event and stop_event.is_set():
                    return records
                records.append(
                    MqttPublishRecord(
                        device_id=device.id,
                        device_name=device.name,
                        seq=seq,
                        success=False,
                        error=str(exc),
                    )
                )
        return records

    for device in devices:
        for seq in range(1, config.telemetry_per_device + 1):
            if stop_event and stop_event.is_set():
                return records
            payload = {
                "runId": run_id,
                "deviceName": device.name,
                "seq": seq,
                "temperature": round(23.5 + seq * 0.3, 2),
                "humidity": 50 + seq,
                "power": round(10.0 + seq * 0.5, 2),
                "tsText": datetime.now().isoformat(timespec="seconds"),
            }
            records.append(publish_device_telemetry(config, device, seq, payload, mqtt=mqtt))
            if stop_event:
                if stop_event.wait(config.telemetry_interval_seconds):
                    return records
            else:
                time.sleep(config.telemetry_interval_seconds)
    return records


def publish_device_telemetry(
    config: RunConfig,
    device: EntityRecord,
    seq: int,
    payload: dict[str, Any],
    *,
    mqtt: Any | None = None,
) -> MqttPublishRecord:
    mqtt = mqtt or _load_mqtt_client()
    username = device.mqtt_username or device.mqtt_password_or_token
    password = device.mqtt_password_or_token if device.mqtt_username else ""
    try:
        client = mqtt.Client(client_id=device.mqtt_client_id)
        if username:
            client.username_pw_set(username, password or None)
        client.connect(config.mqtt_host, config.mqtt_port, keepalive=30)
        client.loop_start()
        info = client.publish(
            "v1/devices/me/telemetry",
            payload=json.dumps(payload, ensure_ascii=False),
            qos=1,
        )
        info.wait_for_publish(timeout=10)
        client.loop_stop()
        client.disconnect()
        success = bool(info.is_published())
        return MqttPublishRecord(
            device_id=device.id,
            device_name=device.name,
            seq=seq,
            success=success,
            error="" if success else "publish did not complete",
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        return MqttPublishRecord(
            device_id=device.id,
            device_name=device.name,
            seq=seq,
            success=False,
            error=str(exc),
            payload=payload,
        )
