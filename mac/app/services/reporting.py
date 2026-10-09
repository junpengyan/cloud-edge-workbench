from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.models.entities import EntityRecord, RunResult
from app.services.import_package import write_platform_import_package


SENSITIVE_KEYS = {
    "sysadmin_password",
    "tenant_admin_password",
    "existing_tenant_admin_password",
}


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def sanitize_config(config_snapshot: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(config_snapshot)
    for key in SENSITIVE_KEYS:
        if key in sanitized:
            sanitized[key] = mask_secret(str(sanitized[key]))
    return sanitized


def result_to_output_dict(result: RunResult) -> dict[str, Any]:
    return {
        "runId": result.run_id,
        "status": result.status,
        "startedAt": result.started_at.isoformat(),
        "finishedAt": result.finished_at.isoformat() if result.finished_at else None,
        "configSnapshot": sanitize_config(result.config_snapshot),
        "tenant": result.tenant.model_dump(mode="json") if result.tenant else None,
        "tenantAdmin": result.tenant_admin.model_dump(mode="json") if result.tenant_admin else None,
        "assets": [item.model_dump(mode="json") for item in result.assets],
        "devices": [item.model_dump(mode="json") for item in result.devices],
        "ruleChains": [item.model_dump(mode="json") for item in result.rule_chains],
        "edges": [item.model_dump(mode="json") for item in result.edges],
        "relations": [item.model_dump(mode="json") for item in result.relations],
        "edgeAssignments": [item.model_dump(mode="json") for item in result.edge_assignments],
        "mqttPublishes": [item.model_dump(mode="json") for item in result.mqtt_publishes],
        "validationChecks": [item.model_dump(mode="json") for item in result.validation_checks],
        "duration": _duration_seconds(result),
        "manualEstimate": result.manual_estimate.model_dump(mode="json") if result.manual_estimate else None,
        "error": result.error,
    }


def write_reports(result: RunResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "entities.json").write_text(
        json.dumps(result_to_output_dict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(result, output_dir / "entities.csv")
    write_platform_import_package(result, output_dir)
    (output_dir / "summary.md").write_text(_build_summary(result), encoding="utf-8")


def _write_csv(result: RunResult, path: Path) -> None:
    rows: list[EntityRecord] = []
    for item in [result.tenant, result.tenant_admin]:
        if item:
            rows.append(item)
    rows.extend(result.assets)
    rows.extend(result.devices)
    rows.extend(result.rule_chains)
    rows.extend(result.edges)

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "entity_type",
                "name",
                "id",
                "type",
                "parent_name",
                "edge_name",
                "mqtt_client_id",
                "mqtt_username",
                "mqtt_password_or_token",
                "notes",
            ],
        )
        writer.writeheader()
        for row in rows:
            data = row.model_dump()
            writer.writerow({field: data.get(field, "") for field in writer.fieldnames or []})


def _build_summary(result: RunResult) -> str:
    estimate = result.manual_estimate
    lines = [
        f"# 快速造数执行报告 - {result.run_id}",
        "",
        f"- 执行状态：{result.status}",
        f"- 开始时间：{result.started_at.isoformat(timespec='seconds')}",
        f"- 结束时间：{result.finished_at.isoformat(timespec='seconds') if result.finished_at else ''}",
        f"- 平台地址：{result.config_snapshot.get('base_url', '')}",
        f"- 输出目录：{result.output_dir}",
        "",
        "## 创建统计",
        "",
        f"- 租户：{1 if result.tenant else 0}",
        f"- 租户管理员：{1 if result.tenant_admin else 0}",
        f"- 资产：{len(result.assets)}",
        f"- 设备：{len(result.devices)}",
        f"- 规则链：{len(result.rule_chains)}",
        f"- 边缘实例：{len(result.edges)}",
        f"- 关系：{len(result.relations)}",
        f"- 边缘分配：{len(result.edge_assignments)}",
        "",
        "## 执行后校验",
        "",
    ]
    if result.validation_checks:
        for item in result.validation_checks:
            status = "通过" if item.success else "异常"
            expected = f"预期 {item.expected}" if item.expected else "无预期值"
            actual = f"实际 {item.actual}" if item.actual else "无实际值"
            detail = f"，{item.details}" if item.details else ""
            lines.append(f"- [{status}] {item.category} / {item.name}：{expected}，{actual}{detail}")
    else:
        lines.append("- 暂无校验记录")
    lines.extend([
        "",
        "## 人力节省估算",
        "",
    ])
    if estimate:
        lines.extend(
            [
                f"- 自动化实际耗时：{estimate.automation_minutes} 分钟",
                f"- 预估人工耗时：{estimate.manual_minutes} 分钟",
                f"- 预计节省时间：{estimate.saved_minutes} 分钟",
                f"- 预计节省比例：{round(estimate.saved_ratio * 100, 2)}%",
            ]
        )
    else:
        lines.append("- 暂无估算")

    if result.error:
        lines.extend(["", "## 错误", "", result.error])

    return "\n".join(lines) + "\n"


def _duration_seconds(result: RunResult) -> float:
    if not result.finished_at:
        return 0.0
    return round((result.finished_at - result.started_at).total_seconds(), 3)
