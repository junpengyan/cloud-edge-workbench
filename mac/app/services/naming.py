from __future__ import annotations

from datetime import datetime
from uuid import uuid4


def make_run_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"run-{timestamp}-{uuid4().hex[:6]}"


def make_entity_name(run_id: str, template_name: str, index: int) -> str:
    short = run_id.replace("run-", "").replace("-", "")[-6:]
    safe_template_name = template_name.strip().replace(" ", "-")
    return f"QD-{short}-{safe_template_name}-{index:03d}"


def make_prefixed_device_name(prefix: str, template_name: str, index: int) -> str:
    safe_prefix = prefix.strip()
    safe_template_name = template_name.strip().replace(" ", "-").lower()
    return f"{safe_prefix}{safe_template_name}{index}"


def make_prefixed_template_name(prefix: str, template_name: str, index: int) -> str:
    safe_prefix = prefix.strip()
    safe_template_name = template_name.strip().replace(" ", "-")
    return f"{safe_prefix}{safe_template_name}{index}"


def make_custom_rule_chain_name(name: str, index: int, count: int) -> str:
    safe_name = name.strip()
    return safe_name if count == 1 else f"{safe_name}-{index}"


def make_auto_tenant_name(run_id: str) -> str:
    return f"QD-Tenant-{run_id[-6:]}"


def make_auto_tenant_admin_email(run_id: str) -> str:
    return f"qd-admin-{run_id[-6:]}@example.local"
