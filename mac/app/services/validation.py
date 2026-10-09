from __future__ import annotations

from app.models.config import RunConfig
from app.models.entities import RunResult, ValidationCheckRecord
from app.services.rule_chain_imports import parse_rule_chain_imports
from app.services.templates import count_template_items, parse_name_list


def build_run_validation(result: RunResult, config: RunConfig) -> list[ValidationCheckRecord]:
    checks = [
        _count_check(
            "资产",
            "资产创建数量",
            count_template_items(config.asset_template) if config.enable_asset_creation else 0,
            len(result.assets),
        ),
        _count_check(
            "设备",
            "已有设备匹配数量" if config.reuse_existing_devices else "设备创建数量",
            count_template_items(config.device_template) if (config.enable_device_creation or config.reuse_existing_devices) else 0,
            len(result.devices),
        ),
        _count_check(
            "规则链",
            "规则链创建数量",
            _expected_rule_chain_count(config) if config.enable_rule_chain_creation else 0,
            len(result.rule_chains),
        ),
        _count_check(
            "边缘",
            "边缘实例创建数量",
            _expected_edge_count(config) if config.enable_edge_creation else 0,
            len(result.edges),
        ),
    ]
    checks.append(_edge_assignment_check(result, config))
    return checks


def _count_check(category: str, name: str, expected: int, actual: int) -> ValidationCheckRecord:
    return ValidationCheckRecord(
        category=category,
        name=name,
        success=expected == actual,
        expected=str(expected),
        actual=str(actual),
    )


def _edge_assignment_check(result: RunResult, config: RunConfig) -> ValidationCheckRecord:
    expected = 0
    if config.enable_edge_assignment and result.edges:
        expected = len(result.devices) + len(result.assets) + sum(1 for item in result.rule_chains if item.type == "EDGE")
    actual = len(result.edge_assignments)
    return ValidationCheckRecord(
        category="边缘",
        name="边缘分配数量",
        success=expected == actual,
        expected=str(expected),
        actual=str(actual),
        details="EDGE 规则链模板会参与边缘分配，CORE 规则链不会分配到边缘。",
    )


def _expected_rule_chain_count(config: RunConfig) -> int:
    imports = parse_rule_chain_imports(config.rule_chain_imports)
    if imports:
        return sum(item.count for item in imports)
    return config.rule_chain_count


def _expected_edge_count(config: RunConfig) -> int:
    names = parse_name_list(config.edge_names)
    return len(names) if names else config.edge_count
