from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.platform.thingsboard_client import ThingsBoardClient, ThingsBoardError


@dataclass
class CleanupPlan:
    tenants: list[str] = field(default_factory=list)
    tenant_names: dict[str, str] = field(default_factory=dict)
    edges: list[str] = field(default_factory=list)
    edge_names: dict[str, str] = field(default_factory=dict)
    rule_chains: list[str] = field(default_factory=list)
    rule_chain_names: dict[str, str] = field(default_factory=dict)
    devices: list[str] = field(default_factory=list)
    device_names: dict[str, str] = field(default_factory=dict)
    assets: list[str] = field(default_factory=list)
    asset_names: dict[str, str] = field(default_factory=dict)
    tenant_fallbacks: dict[str, "EntityCleanupIds"] = field(default_factory=dict)
    report_count: int = 0


@dataclass
class EntityCleanupIds:
    edges: list[str] = field(default_factory=list)
    edge_names: dict[str, str] = field(default_factory=dict)
    rule_chains: list[str] = field(default_factory=list)
    rule_chain_names: dict[str, str] = field(default_factory=dict)
    devices: list[str] = field(default_factory=list)
    device_names: dict[str, str] = field(default_factory=dict)
    assets: list[str] = field(default_factory=list)
    asset_names: dict[str, str] = field(default_factory=dict)


@dataclass
class CleanupResult:
    ok: bool = True
    report_count: int = 0
    preview: bool = False
    planned_tenants: int = 0
    planned_edges: int = 0
    planned_rule_chains: int = 0
    planned_devices: int = 0
    planned_assets: int = 0
    deleted_tenants: int = 0
    deleted_edges: int = 0
    deleted_rule_chains: int = 0
    deleted_devices: int = 0
    deleted_assets: int = 0
    skipped: int = 0
    logs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "report_count": self.report_count,
            "preview": self.preview,
            "planned_tenants": self.planned_tenants,
            "planned_edges": self.planned_edges,
            "planned_rule_chains": self.planned_rule_chains,
            "planned_devices": self.planned_devices,
            "planned_assets": self.planned_assets,
            "deleted_tenants": self.deleted_tenants,
            "deleted_edges": self.deleted_edges,
            "deleted_rule_chains": self.deleted_rule_chains,
            "deleted_devices": self.deleted_devices,
            "deleted_assets": self.deleted_assets,
            "skipped": self.skipped,
            "logs": self.logs,
            "errors": self.errors,
        }


def load_cleanup_plan(output_root: Path, base_url: str = "", run_id: str = "") -> CleanupPlan:
    reports: list[dict[str, Any]] = []
    paths = [output_root / run_id / "entities.json"] if run_id else sorted(output_root.glob("run-*/entities.json"))
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and _report_matches_base_url(data, base_url):
            reports.append(data)
    return build_cleanup_plan(reports)


def build_cleanup_plan(reports: list[dict[str, Any]]) -> CleanupPlan:
    plan = CleanupPlan(report_count=len(reports))
    for report in reports:
        tenant_id = _entity_id(report.get("tenant"))
        if tenant_id:
            tenant_name = _entity_name(report.get("tenant"))
            tenant_id = next(
                (
                    known_id
                    for known_id, known_name in plan.tenant_names.items()
                    if tenant_name and known_name == tenant_name
                ),
                tenant_id,
            )
            _append_unique(plan.tenants, tenant_id)
            plan.tenant_names[tenant_id] = tenant_name
            fallback = plan.tenant_fallbacks.setdefault(tenant_id, EntityCleanupIds())
            _extend_ids(fallback.edges, report.get("edges"), fallback.edge_names)
            _extend_ids(fallback.rule_chains, report.get("ruleChains"), fallback.rule_chain_names)
            _extend_ids(fallback.devices, report.get("devices"), fallback.device_names)
            _extend_ids(fallback.assets, report.get("assets"), fallback.asset_names)
            continue
        _extend_ids(plan.edges, report.get("edges"), plan.edge_names)
        _extend_ids(plan.rule_chains, report.get("ruleChains"), plan.rule_chain_names)
        _extend_ids(plan.devices, report.get("devices"), plan.device_names)
        _extend_ids(plan.assets, report.get("assets"), plan.asset_names)
    return plan


def cleanup_created_data(
    *,
    base_url: str,
    sysadmin_username: str,
    sysadmin_password: str,
    existing_tenant_admin_username: str = "",
    existing_tenant_admin_password: str = "",
    run_id: str = "",
    device_name_prefix: str = "",
    asset_name_prefix: str = "",
    edge_names: str = "",
    rule_chain_name: str = "",
    output_root: Path,
    timeout_seconds: float = 15.0,
    preview: bool = False,
    cleanup_scope: str = "all_reports",
) -> CleanupResult:
    normalized_scope = cleanup_scope if cleanup_scope in {"run", "all_reports", "scan"} else "run"
    requested_run_id = run_id.strip()
    if normalized_scope == "run":
        plan = load_cleanup_plan(output_root, base_url, requested_run_id) if requested_run_id else CleanupPlan()
    elif normalized_scope == "all_reports":
        plan = load_cleanup_plan(output_root, base_url, "")
    else:
        plan = CleanupPlan()
    result = CleanupResult(report_count=plan.report_count, preview=preview)
    scan_filters_provided = any([
        device_name_prefix.strip(),
        asset_name_prefix.strip(),
        edge_names.strip(),
        rule_chain_name.strip(),
    ])
    scan_requested = normalized_scope == "scan" and scan_filters_provided
    if normalized_scope == "run" and not requested_run_id:
        result.ok = False
        result.errors.append("按本次执行清理时必须提供 runId，已停止清理。")
        result.logs.append("未提供 runId，未执行任何前缀扫描或删除。")
        return result
    if not plan.report_count and not scan_requested:
        result.logs.append(
            f"未找到当前平台 {base_url} 对应的执行报告，已停止清理。"
        )
        return result
    if plan.report_count:
        scope_text = f"执行报告 {requested_run_id}" if requested_run_id else f"当前平台 {base_url}"
        result.logs.append(
            f"已扫描{scope_text}的匹配报告 {plan.report_count} 份：租户 {len(plan.tenants)} 个，"
            f"独立边缘实例 {_unique_entity_count(plan.edges, plan.edge_names)} 个，"
            f"规则链 {_unique_entity_count(plan.rule_chains, plan.rule_chain_names)} 条，"
            f"设备 {_unique_entity_count(plan.devices, plan.device_names)} 个，"
            f"资产 {_unique_entity_count(plan.assets, plan.asset_names)} 个。"
            "清理时将按报告记录执行。"
        )
    else:
        result.logs.append(
            f"未使用执行报告，启用显式条件扫描清理。"
        )
    allow_name_relocation = normalized_scope != "run"

    if preview and not scan_requested:
        _update_planned_counts(result, plan)
        result.logs.append(
            "清理预览完成："
            f"租户 {result.planned_tenants} 个，边缘实例 {result.planned_edges} 个，"
            f"规则链 {result.planned_rule_chains} 条，设备 {result.planned_devices} 个，"
            f"资产 {result.planned_assets} 个。"
        )
        return result

    if plan.tenants:
        with ThingsBoardClient(base_url, timeout_seconds=timeout_seconds) as sysadmin_client:
            sysadmin_client.login(sysadmin_username, sysadmin_password)
            for tenant_id, tenant_name in _iter_unique_entities(plan.tenants, plan.tenant_names):
                if allow_name_relocation:
                    status = _delete_with_name_fallback(
                        result, "租户", tenant_id, tenant_name,
                        sysadmin_client.delete_tenant, sysadmin_client.find_tenant_id_by_name, "deleted_tenants",
                    )
                else:
                    status = _delete(result, "租户", tenant_id, sysadmin_client.delete_tenant, "deleted_tenants")
                fallback = plan.tenant_fallbacks.get(tenant_id)
                if status == "missing" and fallback:
                    _merge_cleanup_ids(plan, fallback)

    standalone_count = sum([
        _unique_entity_count(plan.edges, plan.edge_names),
        _unique_entity_count(plan.rule_chains, plan.rule_chain_names),
        _unique_entity_count(plan.devices, plan.device_names),
        _unique_entity_count(plan.assets, plan.asset_names),
    ])
    if standalone_count or scan_requested:
        if not existing_tenant_admin_username or not existing_tenant_admin_password:
            result.ok = False
            result.errors.append(
                "逐项清理当前租户数据时，请填写已有租户管理员账号和密码后再次清理。"
            )
            result.logs.append(
                f"待逐项清理：边缘实例 {_unique_entity_count(plan.edges, plan.edge_names)} 个，"
                f"规则链 {_unique_entity_count(plan.rule_chains, plan.rule_chain_names)} 条，"
                f"设备 {_unique_entity_count(plan.devices, plan.device_names)} 个，"
                f"资产 {_unique_entity_count(plan.assets, plan.asset_names)} 个。"
                "请填写当前云平台中仍存在的已有租户管理员账号和密码后再次清理。"
            )
            return result
        with ThingsBoardClient(base_url, timeout_seconds=timeout_seconds) as tenant_client:
            tenant_client.login(existing_tenant_admin_username, existing_tenant_admin_password)
            if scan_requested:
                _scan_matching_entities(
                    result,
                    plan,
                    tenant_client,
                    device_name_prefix=device_name_prefix,
                    asset_name_prefix=asset_name_prefix,
                    edge_names=edge_names,
                    rule_chain_name=rule_chain_name,
                )
            _update_planned_counts(result, plan)
            if preview:
                result.logs.append(
                    "清理预览完成："
                    f"租户 {result.planned_tenants} 个，边缘实例 {result.planned_edges} 个，"
                    f"规则链 {result.planned_rule_chains} 条，设备 {result.planned_devices} 个，"
                    f"资产 {result.planned_assets} 个。"
                )
                return result
            for entity_id, entity_name in _iter_unique_entities(plan.edges, plan.edge_names):
                if allow_name_relocation:
                    _delete_with_name_fallback(
                        result, "边缘实例", entity_id, entity_name,
                        tenant_client.delete_edge, tenant_client.find_edge_id_by_name, "deleted_edges",
                    )
                else:
                    _delete(result, "边缘实例", entity_id, tenant_client.delete_edge, "deleted_edges")
            for entity_id, entity_name in _iter_unique_entities(plan.rule_chains, plan.rule_chain_names):
                if allow_name_relocation:
                    _delete_with_name_fallback(
                        result, "规则链", entity_id, entity_name,
                        tenant_client.delete_rule_chain, tenant_client.find_rule_chain_id_by_name, "deleted_rule_chains",
                    )
                else:
                    _delete(result, "规则链", entity_id, tenant_client.delete_rule_chain, "deleted_rule_chains")
            for entity_id, entity_name in _iter_unique_entities(plan.devices, plan.device_names):
                if allow_name_relocation:
                    _delete_with_name_fallback(
                        result, "设备", entity_id, entity_name,
                        tenant_client.delete_device, tenant_client.find_device_id_by_name, "deleted_devices",
                    )
                else:
                    _delete(result, "设备", entity_id, tenant_client.delete_device, "deleted_devices")
            for entity_id, entity_name in _iter_unique_entities(plan.assets, plan.asset_names):
                if allow_name_relocation:
                    _delete_with_name_fallback(
                        result, "资产", entity_id, entity_name,
                        tenant_client.delete_asset, tenant_client.find_asset_id_by_name, "deleted_assets",
                    )
                else:
                    _delete(result, "资产", entity_id, tenant_client.delete_asset, "deleted_assets")
    _update_planned_counts(result, plan)
    if preview:
        result.logs.append(
            "清理预览完成："
            f"租户 {result.planned_tenants} 个，边缘实例 {result.planned_edges} 个，"
            f"规则链 {result.planned_rule_chains} 条，设备 {result.planned_devices} 个，"
            f"资产 {result.planned_assets} 个。"
        )
    return result


def _update_planned_counts(result: CleanupResult, plan: CleanupPlan) -> None:
    result.planned_tenants = _unique_entity_count(plan.tenants, plan.tenant_names)
    result.planned_edges = _unique_entity_count(plan.edges, plan.edge_names)
    result.planned_rule_chains = _unique_entity_count(plan.rule_chains, plan.rule_chain_names)
    result.planned_devices = _unique_entity_count(plan.devices, plan.device_names)
    result.planned_assets = _unique_entity_count(plan.assets, plan.asset_names)


def _scan_matching_entities(
    result: CleanupResult,
    plan: CleanupPlan,
    tenant_client: ThingsBoardClient,
    *,
    device_name_prefix: str,
    asset_name_prefix: str,
    edge_names: str,
    rule_chain_name: str,
) -> None:
    device_prefix = device_name_prefix.strip()
    asset_prefix = asset_name_prefix.strip()
    rule_chain_prefix = rule_chain_name.strip()
    edge_name_list = _parse_name_list(edge_names)
    devices = tenant_client.find_device_ids_by_prefix(device_prefix)
    assets = tenant_client.find_asset_ids_by_prefix(asset_prefix)
    edges = tenant_client.find_edge_ids_by_names(edge_name_list)
    rule_chains = tenant_client.find_rule_chain_ids_by_prefix(rule_chain_prefix)
    _merge_name_matches(plan.devices, plan.device_names, devices)
    _merge_name_matches(plan.assets, plan.asset_names, assets)
    _merge_name_matches(plan.edges, plan.edge_names, edges)
    _merge_name_matches(plan.rule_chains, plan.rule_chain_names, rule_chains)
    result.logs.append(
        "显式条件扫描结果："
        f"设备前缀 {device_prefix or '未填写'} -> {len(devices)} 个，"
        f"资产前缀 {asset_prefix or '未填写'} -> {len(assets)} 个，"
        f"边缘实例名称 {len(edge_name_list)} 个 -> {len(edges)} 个，"
        f"规则链名称前缀 {rule_chain_prefix or '未填写'} -> {len(rule_chains)} 条。"
    )


def _delete(
    result: CleanupResult,
    label: str,
    entity_id: str,
    delete: Callable[[str], dict[str, Any]],
    count_field: str,
) -> str:
    try:
        delete(entity_id)
        setattr(result, count_field, getattr(result, count_field) + 1)
        result.logs.append(f"已删除{label}：{entity_id}")
        return "deleted"
    except ThingsBoardError as exc:
        if " returned 404:" in str(exc):
            result.skipped += 1
            result.logs.append(f"{label}已不存在，跳过：{entity_id}")
            return "missing"
        result.ok = False
        message = f"删除{label}失败：{entity_id}，{exc}"
        result.errors.append(message)
        result.logs.append(message)
        return "failed"


def _delete_with_name_fallback(
    result: CleanupResult,
    label: str,
    entity_id: str,
    entity_name: str,
    delete: Callable[[str], dict[str, Any]],
    find_id_by_name: Callable[[str], str],
    count_field: str,
) -> str:
    if not entity_name:
        return _delete(result, label, entity_id, delete, count_field)
    current_id = find_id_by_name(entity_name)
    if not current_id:
        result.skipped += 1
        result.logs.append(f"平台中未找到{label}，跳过：{entity_name}")
        return "missing"
    if current_id != entity_id:
        result.logs.append(f"{label} ID 已变化，按名称重新定位：{entity_name} -> {current_id}")
    return _delete(result, label, current_id, delete, count_field)


def _iter_unique_entities(entity_ids: list[str], entity_names: dict[str, str]):
    seen_names: set[str] = set()
    for entity_id in entity_ids:
        entity_name = entity_names.get(entity_id, "")
        if entity_name:
            if entity_name in seen_names:
                continue
            seen_names.add(entity_name)
        yield entity_id, entity_name


def _unique_entity_count(entity_ids: list[str], entity_names: dict[str, str]) -> int:
    return sum(1 for _ in _iter_unique_entities(entity_ids, entity_names))


def _merge_cleanup_ids(plan: CleanupPlan, fallback: EntityCleanupIds) -> None:
    for target, source, target_names, source_names in [
        (plan.edges, fallback.edges, plan.edge_names, fallback.edge_names),
        (plan.rule_chains, fallback.rule_chains, plan.rule_chain_names, fallback.rule_chain_names),
        (plan.devices, fallback.devices, plan.device_names, fallback.device_names),
        (plan.assets, fallback.assets, plan.asset_names, fallback.asset_names),
    ]:
        for entity_id in source:
            _append_unique(target, entity_id)
            target_names[entity_id] = source_names.get(entity_id, "")


def _extend_ids(target: list[str], raw_entities: Any, names: dict[str, str] | None = None) -> None:
    if not isinstance(raw_entities, list):
        return
    for entity in raw_entities:
        entity_id = _entity_id(entity)
        if entity_id:
            _append_unique(target, entity_id)
            if names is not None:
                names[entity_id] = _entity_name(entity)


def _entity_id(raw_entity: Any) -> str:
    if not isinstance(raw_entity, dict):
        return ""
    return str(raw_entity.get("id") or "")


def _entity_name(raw_entity: Any) -> str:
    if not isinstance(raw_entity, dict):
        return ""
    return str(raw_entity.get("name") or "")


def _append_unique(target: list[str], entity_id: str) -> None:
    if entity_id not in target:
        target.append(entity_id)


def _merge_name_matches(target: list[str], names: dict[str, str], matches: dict[str, str]) -> None:
    for entity_id, entity_name in matches.items():
        _append_unique(target, entity_id)
        names[entity_id] = entity_name


def _parse_name_list(raw_names: str) -> list[str]:
    return [
        item.strip()
        for line in raw_names.splitlines()
        for item in line.split(",")
        if item.strip()
    ]


def _report_matches_base_url(report: dict[str, Any], base_url: str) -> bool:
    if not base_url:
        return True
    config_snapshot = report.get("configSnapshot")
    if not isinstance(config_snapshot, dict):
        return False
    report_base_url = str(config_snapshot.get("base_url") or "")
    return _normalize_base_url(report_base_url) == _normalize_base_url(base_url)


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/").lower()
