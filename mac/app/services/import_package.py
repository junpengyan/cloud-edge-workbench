from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from app.models.entities import EntityRecord, RelationRecord, RunResult


IMPORT_PACKAGE_DIR_NAME = "平台导入包"
IMPORT_PACKAGE_ZIP_NAME = "平台导入包.zip"

ASSET_FIELDNAMES = [
    "name",
    "type",
    "businessType",
    "stationId",
    "allInstallCapcity",
    "allInstallPower",
    "address",
    "installCapacity",
    "latitude",
    "longitude",
]
DEVICE_FIELDNAMES = [
    "name",
    "type",
    "id",
    "user",
    "password",
    "businessType",
    "stationId",
    "onoff",
]
RELATION_FIELDNAMES = [
    "fromType",
    "fromName",
    "toType",
    "toName",
    "type",
    "typeGroup",
    "additionalInfo",
]


def write_platform_import_package(result: RunResult, output_dir: Path) -> Path | None:
    if not result.assets and not result.devices and not result.relations:
        return None

    package_dir = output_dir / IMPORT_PACKAGE_DIR_NAME
    package_dir.mkdir(parents=True, exist_ok=True)

    written_files: list[Path] = []
    written_files.extend(_write_assets(result.assets, package_dir))
    written_files.extend(_write_devices(result.devices, package_dir))
    written_files.extend(_write_relations(result.relations, package_dir))
    manifest_path = _write_manifest(result, package_dir, written_files)
    written_files.append(manifest_path)

    zip_path = output_dir / IMPORT_PACKAGE_ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in written_files:
            archive.write(path, path.relative_to(output_dir))
    return zip_path


def _write_assets(assets: list[EntityRecord], package_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for asset_type, rows in _group_assets_by_import_type(assets).items():
        path = package_dir / f"{len(rows)}_asset-{_safe_filename(asset_type)}.csv"
        _write_dict_rows(path, ASSET_FIELDNAMES, [_asset_row(item) for item in rows])
        paths.append(path)
    return paths


def _write_devices(devices: list[EntityRecord], package_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for device_type, rows in _group_devices_by_import_type(devices).items():
        path = package_dir / f"{len(rows)}_{_safe_filename(device_type)}.csv"
        _write_dict_rows(path, DEVICE_FIELDNAMES, [_device_row(item) for item in rows])
        paths.append(path)
    return paths


def _write_relations(relations: list[RelationRecord], package_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for group_key, rows in _group_relations(relations).items():
        path = package_dir / f"{group_key}_relation.csv"
        _write_dict_rows(path, RELATION_FIELDNAMES, [_relation_row(item) for item in rows])
        paths.append(path)
    return paths


def _write_manifest(result: RunResult, package_dir: Path, files: list[Path]) -> Path:
    path = package_dir / "import-manifest.json"
    payload = {
        "runId": result.run_id,
        "status": result.status,
        "baseUrl": result.config_snapshot.get("base_url", ""),
        "generatedFiles": [item.name for item in files],
        "counts": {
            "assets": len(result.assets),
            "devices": len(result.devices),
            "relations": len(result.relations),
        },
        "importOrder": [
            "assets",
            "devices",
            "relations",
        ],
        "notes": [
            "CSV files are generated for the platform's asset, device and relation import pages.",
            "Relations use fromName/toName so they can be imported after database rebuilds when entity IDs change.",
            "stationId is preserved only when it exists in source data; otherwise it is left blank to avoid stale IDs.",
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _asset_row(asset: EntityRecord) -> dict[str, str]:
    raw = _flatten_raw(asset.raw)
    asset_type = _asset_import_type(asset)
    return {
        "name": asset.name,
        "type": asset_type,
        "businessType": raw.get("businessType", ""),
        "stationId": raw.get("stationId", ""),
        "allInstallCapcity": raw.get("allInstallCapcity", ""),
        "allInstallPower": raw.get("allInstallPower", ""),
        "address": raw.get("address", ""),
        "installCapacity": raw.get("installCapacity", ""),
        "latitude": raw.get("latitude", ""),
        "longitude": raw.get("longitude", ""),
    }


def _device_row(device: EntityRecord) -> dict[str, str]:
    raw = _flatten_raw(device.raw)
    device_type = _device_import_type(device)
    username = device.mqtt_username or raw.get("user", "") or device.name
    password = device.mqtt_password_or_token or raw.get("password", "") or device.name
    return {
        "name": device.name,
        "type": device_type,
        "id": raw.get("id", "") or device.mqtt_client_id or device.name,
        "user": username,
        "password": password,
        "businessType": raw.get("businessType", "") or _business_type_for_device(device),
        "stationId": raw.get("stationId", ""),
        "onoff": raw.get("onoff", ""),
    }


def _relation_row(relation: RelationRecord) -> dict[str, str]:
    return {
        "fromType": relation.from_entity_type,
        "fromName": relation.from_name,
        "toType": relation.to_entity_type,
        "toName": relation.to_name,
        "type": relation.relation_type,
        "typeGroup": "COMMON",
        "additionalInfo": json.dumps(relation.additional_info, ensure_ascii=False),
    }


def _group_assets_by_import_type(entities: list[EntityRecord]) -> dict[str, list[EntityRecord]]:
    groups: dict[str, list[EntityRecord]] = {}
    for entity in entities:
        key = _asset_import_type(entity)
        groups.setdefault(key, []).append(entity)
    return groups


def _group_devices_by_import_type(entities: list[EntityRecord]) -> dict[str, list[EntityRecord]]:
    groups: dict[str, list[EntityRecord]] = {}
    for entity in entities:
        key = _device_import_type(entity)
        groups.setdefault(key, []).append(entity)
    return groups


def _group_relations(relations: list[RelationRecord]) -> dict[str, list[RelationRecord]]:
    groups: dict[str, list[RelationRecord]] = {}
    for relation in relations:
        info_type = str(relation.additional_info.get("type") or "").strip()
        if info_type:
            key = _safe_filename(info_type)
        else:
            key = f"{_safe_filename(relation.from_entity_type.lower())}-{_safe_filename(relation.to_entity_type.lower())}"
        groups.setdefault(key, []).append(relation)
    return groups


def _write_dict_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _stringify(row.get(field, "")) for field in fieldnames})


def _flatten_raw(raw: dict[str, Any]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            continue
        flattened[key] = _stringify(value)
    return flattened


def _business_type_for_device(device: EntityRecord) -> str:
    template = _note_value(device.notes, "template")
    normalized_template = template.strip().replace("-", "").replace("_", "").replace(" ", "").lower()
    name = f"{device.type} {template} {device.name}".strip().lower()
    if "groupbms" in name:
        return "groupbms"
    if "pcs" in name:
        return "pcs"
    if "bms" in name:
        return "bms"
    if "cell" in name:
        return "cell"
    if "box" in name or "箱变" in device.name:
        return "box"
    if normalized_template in {"cl", "strategy", "策略"} or "策略" in name or "strategy" in name:
        return "strategy"
    return normalized_template or "default"


def _asset_import_type(asset: EntityRecord) -> str:
    if asset.type and asset.type != "default":
        return asset.type
    template = _note_value(asset.notes, "template")
    normalized = template.strip().replace("-", "").replace("_", "").replace(" ", "").lower()
    aliases = {
        "公司": "Company",
        "company": "Company",
        "电站": "powerStation",
        "station": "powerStation",
        "powerstation": "powerStation",
        "储能单元": "energyStationUnit",
        "unit": "energyStationUnit",
        "container": "energyStationUnit",
    }
    return aliases.get(normalized, asset.type or "default")


def _device_import_type(device: EntityRecord) -> str:
    if device.type and device.type != "default":
        return device.type
    return _business_type_for_device(device)


def _note_value(notes: str, wanted_key: str) -> str:
    for part in notes.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == wanted_key:
            return value.strip()
    return ""


def _safe_filename(value: str) -> str:
    safe = value.strip() or "default"
    safe = safe.replace(" ", "-").replace("_", "-")
    safe = re.sub(r'[<>:"/\\|?*]+', "-", safe)
    safe = re.sub(r"-{2,}", "-", safe).strip("-")
    return safe or "default"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
