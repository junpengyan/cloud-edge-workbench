from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


EntityType = Literal["TENANT", "USER", "ASSET", "DEVICE", "RULE_CHAIN", "EDGE"]
RunStatus = Literal["PENDING", "RUNNING", "SUCCESS", "FAILED"]


class TemplateItem(BaseModel):
    name: str
    count: int
    type: str = "default"


class EntityRecord(BaseModel):
    entity_type: EntityType
    name: str
    id: str = ""
    type: str = ""
    parent_name: str = ""
    edge_name: str = ""
    mqtt_client_id: str = ""
    mqtt_username: str = ""
    mqtt_password_or_token: str = ""
    notes: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class RelationRecord(BaseModel):
    from_entity_type: str
    from_id: str
    from_name: str
    to_entity_type: str
    to_id: str
    to_name: str
    relation_type: str
    additional_info: dict[str, Any] = Field(default_factory=dict)


class EdgeAssignmentRecord(BaseModel):
    edge_id: str
    edge_name: str
    entity_type: str
    entity_id: str
    entity_name: str
    success: bool = True
    error: str = ""


class MqttPublishRecord(BaseModel):
    device_id: str
    device_name: str
    seq: int
    success: bool
    topic: str = "v1/devices/me/telemetry"
    error: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ValidationCheckRecord(BaseModel):
    category: str
    name: str
    success: bool
    expected: str = ""
    actual: str = ""
    details: str = ""


class ManualEstimate(BaseModel):
    manual_minutes: float
    automation_minutes: float
    saved_minutes: float
    saved_ratio: float


class RunResult(BaseModel):
    run_id: str
    status: RunStatus = "PENDING"
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    tenant: EntityRecord | None = None
    tenant_admin: EntityRecord | None = None
    assets: list[EntityRecord] = Field(default_factory=list)
    devices: list[EntityRecord] = Field(default_factory=list)
    rule_chains: list[EntityRecord] = Field(default_factory=list)
    edges: list[EntityRecord] = Field(default_factory=list)
    relations: list[RelationRecord] = Field(default_factory=list)
    edge_assignments: list[EdgeAssignmentRecord] = Field(default_factory=list)
    mqtt_publishes: list[MqttPublishRecord] = Field(default_factory=list)
    validation_checks: list[ValidationCheckRecord] = Field(default_factory=list)
    manual_estimate: ManualEstimate | None = None
    output_dir: str = ""
    error: str = ""
