from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DEFAULT_ASSET_TEMPLATE = "公司,1\n电站,1\n储能单元,3"
DEFAULT_DEVICE_TEMPLATE = (
    "PCS,2\n"
    "BMS,2\n"
    "GroupBMS,4\n"
    "Cell,8\n"
    "策略,1\n"
    "箱变,1"
)
DEFAULT_RELATION_RULES = (
    'A1,ASSET,A2,ASSET,auto,Contains,{"type":"Station"}\n'
    'A2,ASSET,A3,ASSET,auto,Contains,{"type":"Container"}\n'
    'A3,ASSET,D1,DEVICE,auto,Contains,{"type":"PCS"}\n'
    'A3,ASSET,D2,DEVICE,auto,Contains,{"type":"BMS"}\n'
    'D2,DEVICE,D3,DEVICE,auto,Contains,{"type":"GroupBMS"}\n'
    'D3,DEVICE,D4,DEVICE,auto,Contains,{"type":"Cell"}\n'
    'A2,ASSET,D5,DEVICE,auto,Contains,{"type":"strategy"}\n'
    'A2,ASSET,D6,DEVICE,auto,Contains,{"type":"BOX"}'
)
DEFAULT_PASSWORD = ""


class RunConfig(BaseModel):
    """User supplied configuration for one automation run."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    base_url: str = ""
    sysadmin_username: str = ""
    sysadmin_password: str = ""
    tenant_name: str = ""
    tenant_admin_email: str = ""
    tenant_admin_password: str = DEFAULT_PASSWORD
    existing_tenant_admin_username: str = ""
    existing_tenant_admin_password: str = ""
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_credentials_mode: Literal["USERNAME", "CLIENT_ID", "CLIENT_ID_USERNAME"] = "USERNAME"
    enable_telemetry: bool = False
    telemetry_per_device: int = 1
    telemetry_interval_seconds: float = 1.0
    enable_asset_creation: bool = True
    enable_device_creation: bool = True
    reuse_existing_devices: bool = False
    enable_relation_creation: bool = True
    edge_mode: Literal["create", "existing", "none"] = "create"
    enable_edge_creation: bool = True
    enable_edge_assignment: bool = True
    edge_count: int = 1
    edge_names: str = ""
    enable_rule_chain_creation: bool = True
    rule_chain_count: int = 1
    rule_chain_type: str = "CORE"
    rule_chain_template: str = "rpc_test"
    rule_chain_name: str = ""
    rule_chain_imports: str = ""
    local_rule_chain_templates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    asset_name_prefix: str = ""
    device_name_prefix: str = ""
    asset_template: str = DEFAULT_ASSET_TEMPLATE
    device_template: str = DEFAULT_DEVICE_TEMPLATE
    relation_type: str = "Contains"
    relation_rules: str = DEFAULT_RELATION_RULES
    output_root: str = "outputs"
    request_timeout_seconds: float = 15.0
    precheck_timeout_seconds: float = 5.0

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http or https URL")
        return value.rstrip("/")

    @field_validator(
        "mqtt_port",
        "telemetry_per_device",
        "rule_chain_count",
    )
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("value must be >= 1")
        return value

    @field_validator("edge_count")
    @classmethod
    def validate_non_negative_edge_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be >= 0")
        return value

    @field_validator("request_timeout_seconds", "precheck_timeout_seconds")
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout must be positive")
        return value

    @field_validator("telemetry_interval_seconds")
    @classmethod
    def validate_non_negative_float(cls, value: float) -> float:
        if value < 0:
            raise ValueError("telemetry interval must be >= 0")
        return value

    @model_validator(mode="after")
    def fill_mqtt_host(self) -> "RunConfig":
        if not self.mqtt_host:
            parsed = urlparse(self.base_url)
            self.mqtt_host = parsed.hostname or "127.0.0.1"
        return self

    @model_validator(mode="after")
    def validate_enabled_edge_creation(self) -> "RunConfig":
        if self.edge_mode == "none":
            object.__setattr__(self, "enable_edge_creation", False)
            object.__setattr__(self, "enable_edge_assignment", False)
        elif self.edge_mode == "existing":
            object.__setattr__(self, "enable_edge_creation", False)
            object.__setattr__(self, "enable_edge_assignment", True)
        if self.enable_edge_creation and not self.edge_names.strip() and self.edge_count < 1:
            raise ValueError("edge_count must be >= 1 when edge creation is enabled")
        if self.enable_edge_assignment and not self.enable_edge_creation and not self.edge_names.strip():
            raise ValueError("edge_names is required when assigning to existing edge instances")
        return self

    @property
    def uses_existing_tenant_admin(self) -> bool:
        return bool(self.existing_tenant_admin_username and self.existing_tenant_admin_password)

    @property
    def output_root_path(self) -> Path:
        return Path(self.output_root)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "RunConfig":
        import json

        with Path(path).open("r", encoding="utf-8") as handle:
            data: dict[str, Any] = json.load(handle)
        return cls.model_validate(data)


class PrecheckRequest(BaseModel):
    base_url: str
    timeout_seconds: float = 5.0
    sysadmin_username: str = ""
    sysadmin_password: str = ""

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http or https URL")
        return value.rstrip("/")


class CleanupRequest(BaseModel):
    base_url: str
    sysadmin_username: str
    sysadmin_password: str
    existing_tenant_admin_username: str = ""
    existing_tenant_admin_password: str = ""
    run_id: str = ""
    device_name_prefix: str = ""
    asset_name_prefix: str = ""
    edge_names: str = ""
    rule_chain_name: str = ""
    cleanup_scope: Literal["run", "all_reports", "scan"] = "run"
    timeout_seconds: float = 15.0
    preview: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http or https URL")
        return value.rstrip("/")

    @field_validator("timeout_seconds")
    @classmethod
    def validate_positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout must be positive")
        return value
