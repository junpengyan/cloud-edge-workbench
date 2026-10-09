from __future__ import annotations

from app.models.entities import ManualEstimate


def estimate_manual_savings(
    *,
    edge_count: int,
    device_count: int,
    asset_count: int,
    rule_chain_count: int,
    relation_count: int,
    edge_assignment_count: int,
    mqtt_publish_count: int,
    automation_seconds: float,
) -> ManualEstimate:
    manual_minutes = (
        5.0
        + edge_count * 2.0
        + device_count * 2.0
        + asset_count * 1.5
        + rule_chain_count * 3.0
        + relation_count * 0.5
        + edge_assignment_count * 0.5
        + mqtt_publish_count * 0.5
    )
    automation_minutes = round(automation_seconds / 60.0, 2)
    saved_minutes = max(round(manual_minutes - automation_minutes, 2), 0.0)
    saved_ratio = 0.0 if manual_minutes <= 0 else round(saved_minutes / manual_minutes, 4)
    return ManualEstimate(
        manual_minutes=round(manual_minutes, 2),
        automation_minutes=automation_minutes,
        saved_minutes=saved_minutes,
        saved_ratio=saved_ratio,
    )

