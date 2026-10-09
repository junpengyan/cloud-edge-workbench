from __future__ import annotations

import json
import re
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE.parents[1]
from .template_parser import parse_template  # noqa: E402


DEFAULT_CONFIG: Dict[str, Any] = {
    "projectPrefix": "test",
    "projectCount": 6,
    "maxProjectPoints": 100000,
    "maxForwardTags": 6000,
    "forwardMode": "direct",
    "mqttHost": "127.0.0.1",
    "mqttPort": 1883,
    "mqttCredentialPrefix": "gn",
    "mqttClientIdPattern": "gn-{type}{index}",
    "mqttUsernamePattern": "gn-{type}{index}",
    "mqttPasswordPattern": "gn-{type}{index}",
    "mqttSn": "TN001",
    "mqttQos": 0,
    "mqttPub": "v1/devices/me/telemetry",
    "mqttSub": "v1/devices/me/rpc/request/+",
    "mqttPub2": "pub2",
    "changePeriod": 1,
    "pubTimer": 300,
    "templateDbPath": str(WORKSPACE / "db.xml"),
    "templateBmsCsvPath": str(WORKSPACE / "C219_D01.csv"),
    "alarmRules": [
        {"type": "PCS", "enabled": True, "description": "PCS_zlzhedxj15V1_YX", "generatorType": 4, "interval": 3600000, "var1": "", "var2": "0"},
        {"type": "BMS", "enabled": True, "description": "BMS_jgzmkgwdgj1_YX", "generatorType": 4, "interval": 3600000, "var1": "", "var2": "0"},
    ],
}


DEFAULT_TYPES: List[Dict[str, Any]] = [
    {"type": "PCS", "prefix": "pcs", "points": 290, "secondPoints": 58, "intervalRules": "1000:58,86400000:*", "total": 3700, "projects": []},
    {"type": "BMS", "prefix": "bms", "points": 290, "secondPoints": 58, "intervalRules": "1000:58,86400000:*", "total": 3130, "projects": []},
    {"type": "GROUPBMS", "prefix": "groupbms", "points": 100, "secondPoints": 20, "intervalRules": "1000:20,86400000:*", "total": 3130, "projects": []},
    {"type": "CELL", "prefix": "cell", "points": 900, "secondPoints": 180, "intervalRules": "1000:180,86400000:*", "total": 3130, "projects": []},
    {"type": "CL", "prefix": "cl", "points": 11100, "secondPoints": 2220, "intervalRules": "1000:2220,86400000:*", "total": 5, "projects": []},
    {"type": "BOX", "prefix": "box", "points": 130, "secondPoints": 26, "intervalRules": "1000:26,86400000:*", "total": 260, "projects": []},
]

BMS_DERIVED_TYPES = {"BMS", "GROUPBMS", "CELL"}
for _default_row in DEFAULT_TYPES:
    _default_row['changeRate'] = 20

SECOND_LEVEL_INTERVAL = "1000"
DAY_LEVEL_INTERVAL = "86400000"

def alarm_rules(config):
    rules = config.get('alarmRules', DEFAULT_CONFIG['alarmRules'])
    if not isinstance(rules, list):
        raise ValueError('告警测点规则格式无效')
    result, seen = [], set()
    for raw in rules:
        kind = raw.get('type')
        if kind not in {'PCS', 'BMS'} or kind in seen:
            raise ValueError('PCS、BMS各支持一条告警测点规则，设备类型不能重复')
        seen.add(kind)
        if not isinstance(raw.get('enabled'), bool):
            raise ValueError(f'{kind}告警规则启用状态无效')
        if not raw['enabled']:
            continue
        desc = str(raw.get('description') or '').strip()
        if not desc:
            raise ValueError(f'{kind}告警测点描述不能为空')
        values = {}
        for key, maximum in [('generatorType', 6), ('interval', 2147483647)]:
            value = str(raw.get(key, ''))
            if not re.fullmatch(r'\d+', value) or not (0 if key=='generatorType' else 1) <= int(value) <= maximum:
                raise ValueError(f'{kind}告警规则 {key} 无效')
            values[key] = int(value)
        result.append({**raw, **values, 'description': desc})
    return result


def apply_alarm_rule(tags, kind, config):
    result = [dict(t) for t in tags]
    for rule in alarm_rules(config):
        if rule['type'] != kind:
            continue
        matches = [t for t in result if t['d'] == rule['description']]
        if len(matches) != 1:
            raise ValueError(f"{kind}告警测点“{rule['description']}”匹配{len(matches)}个，必须匹配唯一采集点描述")
        matches[0].update(s0=str(rule['generatorType']), p=str(rule['interval']),
                          s2=str(rule.get('var1', '')), s1=str(rule.get('var2', '')))
    return result


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def point_value_type(name: str) -> int:
    return 0 if re.search(r"_YX$", name or "", re.I) else 1


def template_tags(config: Dict[str, Any]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    messages: List[str] = []
    templates: Dict[str, List[Dict[str, Any]]] = {}
    db_path = Path(str(config.get("templateDbPath") or DEFAULT_CONFIG["templateDbPath"]))
    bms_path = Path(str(config.get("templateBmsCsvPath") or DEFAULT_CONFIG["templateBmsCsvPath"]))

    if db_path.exists():
        parsed = parse_template(db_path.read_bytes(), db_path.name)
        for key, tpl in (parsed.get("templates") or {}).items():
            templates[str(key).upper()] = [dict(t) for t in (tpl.get("tags") or [])]
        messages.append(f"已加载 db.xml 模板：{', '.join(sorted(templates))}")
    else:
        messages.append(f"未找到 db.xml 模板：{db_path}")

    if bms_path.exists():
        parsed = parse_template(bms_path.read_bytes(), bms_path.name)
        bms_tpl = (parsed.get("templates") or {}).get("BMS")
        if bms_tpl:
            templates["BMS"] = [dict(t) for t in (bms_tpl.get("tags") or [])]
            messages.append(f"已加载 BMS CSV 模板：{bms_path.name}")
    else:
        messages.append(f"未找到 BMS CSV 模板：{bms_path}")

    return templates, messages


def normalize_tag(raw: Dict[str, Any], index: int, fallback_prefix: str) -> Dict[str, Any]:
    name = str(raw.get("n") or raw.get("tag") or f"Tag{index}")
    desc = str(raw.get("d") or raw.get("name") or f"{fallback_prefix}_Tag{index}")
    return {
        "n": name,
        "d": desc,
        "r": str(raw.get("r") or raw.get("read_write") or "0"),
        "p": str(raw.get("p") or raw.get("interval") or "1000"),
        "u": str(raw.get("u") or ""),
        "s0": str(raw.get("s0") or raw.get("generator_type") or "0"),
        "s1": str(raw.get("s1") or raw.get("var2") or ""),
        "s2": str(raw.get("s2") or raw.get("var1") or ""),
    }


def fallback_tags(kind: str, points: int) -> List[Dict[str, Any]]:
    prefix = kind.upper()
    return [{"n": f"Tag{i}", "d": f"{prefix}_Tag{i}", "r": "0", "p": "1000", "u": "", "s0": "0", "s1": "", "s2": ""} for i in range(1, points + 1)]


def extend_tags(tags: List[Dict[str, Any]], kind: str, points: int) -> List[Dict[str, Any]]:
    normalized = [normalize_tag(t, i, kind.upper()) for i, t in enumerate(tags[:points], start=1)]
    if len(normalized) >= points:
        return normalized
    used_names = {tag['n'] for tag in normalized}
    used_descriptions = {tag['d'] for tag in normalized}
    next_no = len(normalized) + 1
    while len(normalized) < points:
        name, desc = f'Tag{next_no}', f'{kind.upper()}_Tag{next_no}'
        next_no += 1
        if name in used_names or desc in used_descriptions:
            continue
        normalized.append({'n':name, 'd':desc, 'r':'0', 'p':'1000', 'u':'', 's0':'0', 's1':'', 's2':''})
        used_names.add(name)
        used_descriptions.add(desc)
    return normalized


def bms_segments(tags: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    segments = {"bms": [], "groupbms": [], "cell": []}
    for tag in tags:
        desc = str(tag.get("d") or "")
        if re.match(r"^GroupBMS_", desc, re.I):
            segments["groupbms"].append(tag)
        elif re.match(r"^Cell_", desc, re.I):
            segments["cell"].append(tag)
        else:
            segments["bms"].append(tag)
    return segments


def default_interval_rules(row: Dict[str, Any]) -> str:
    points = max(0, to_int(row.get("points"), 0))
    second_points = max(0, min(points, to_int(row.get("secondPoints"), points // 5)))
    return f"{SECOND_LEVEL_INTERVAL}:{second_points},{DAY_LEVEL_INTERVAL}:*"


def automatic_interval_rules(row):
    try:
        points = Decimal(str(row.get('points', '')))
        rate = Decimal(str(row.get('changeRate', '')))
        if not points.is_finite() or points < 0 or points != points.to_integral_value():
            raise ValueError('单设备点数必须是非负整数')
        if not rate.is_finite() or not 0 <= rate <= 100 or rate != rate.quantize(Decimal('0.01')):
            raise ValueError('突变率必须为0至100，最多两位小数')
    except (InvalidOperation, TypeError):
        raise ValueError('请填写有效的单设备点数和突变率')
    seconds = int(points * rate / 100)
    return seconds, f'{SECOND_LEVEL_INTERVAL}:{seconds},{DAY_LEVEL_INTERVAL}:*'


def parse_interval_rules(row: Dict[str, Any]) -> List[Tuple[str, int | None]]:
    raw = automatic_interval_rules(row)[1] if 'changeRate' in row else str(row.get("intervalRules") or "").strip()
    if not raw:
        raw = default_interval_rules(row)
    rules: List[Tuple[str, int | None]] = []
    for part in re.split(r"[,，;；\n]+", raw):
        text = part.strip()
        if not text:
            continue
        if ":" in text:
            interval, count = text.split(":", 1)
        elif "=" in text:
            interval, count = text.split("=", 1)
        else:
            continue
        interval = interval.strip()
        count = count.strip()
        if not interval:
            continue
        rules.append((interval, None if count in {"*", "剩余", "rest", "REST"} else max(0, to_int(count, 0))))
    return rules or [(SECOND_LEVEL_INTERVAL, max(0, to_int(row.get("secondPoints"), 0))), (DAY_LEVEL_INTERVAL, None)]


def interval_for_index(index: int, rules: List[Tuple[str, int | None]]) -> str:
    consumed = 0
    fallback = DAY_LEVEL_INTERVAL
    for interval, count in rules:
        fallback = interval
        if count is None:
            return interval
        consumed += count
        if index <= consumed:
            return interval
    return fallback


def apply_interval_rule(tags: List[Dict[str, Any]], row: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = parse_interval_rules(row)
    result: List[Dict[str, Any]] = []
    for index, tag in enumerate(tags, start=1):
        item = dict(tag)
        item["p"] = interval_for_index(index, rules)
        result.append(item)
    return result


def apply_bms_interval_rule(tags: List[Dict[str, Any]], rows_by_type: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    counters = {"BMS": 0, "GROUPBMS": 0, "CELL": 0}
    rules_by_type = {kind: parse_interval_rules(rows_by_type.get(kind, {})) for kind in counters}
    result: List[Dict[str, Any]] = []
    for tag in tags:
        desc = str(tag.get("d") or "")
        if re.match(r"^GroupBMS_", desc, re.I):
            kind = "GROUPBMS"
        elif re.match(r"^Cell_", desc, re.I):
            kind = "CELL"
        else:
            kind = "BMS"
        counters[kind] += 1
        item = dict(tag)
        item["p"] = interval_for_index(counters[kind], rules_by_type[kind])
        result.append(item)
    return result


def collect_point(tag: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tag": str(tag.get("n") or ""),
        "name": str(tag.get("d") or ""),
        "flag": True,
        "generator_type": to_int(tag.get("s0"), 0),
        "var1": str(tag.get("s2") or ""),
        "var2": str(tag.get("s1") or ""),
        "interval": to_int(tag.get("p"), 1000),
        "use_formula": False,
        "coefficient": 1.0,
        "offset": 0,
        "read_write": to_int(tag.get("r"), 0),
        "valueType": point_value_type(str(tag.get("d") or "")),
    }


def device_status_point() -> Dict[str, Any]:
    return {
        "tag": "device_status",
        "name": "设备状态",
        "flag": True,
        "generator_type": 0,
        "var1": "",
        "var2": "",
        "interval": 1000,
        "use_formula": False,
        "coefficient": 1.0,
        "offset": 0,
        "read_write": 0,
        "valueType": 0,
    }


def mqtt_server(config: Dict[str, Any], credentials: Dict[str, str], point_table: List[Dict[str, Any]], channel_name: str, alias: str) -> Dict[str, Any]:
    device = {
        "change": to_int(config.get("changePeriod"), 1),
        "interval": to_int(config.get("pubTimer"), 300),
        "offset": 0,
        "point_table": point_table,
        "deviceName": channel_name,
        "alias_name": alias,
        "used": True,
    }
    return {
        "mqtt_version": 4,
        "host": str(config.get("mqttHost") or "127.0.0.1"),
        "port": to_int(config.get("mqttPort"), 1883),
        "client_id": credentials["client_id"],
        "user_name": credentials["user_name"],
        "pass_word": credentials["pass_word"],
        "sn_info": str(config.get("mqttSn") or "TN001"),
        "minPackSendDelayMS": 0,
        "qos": to_int(config.get("mqttQos"), 0),
        "storage_read_records_count": 1000,
        "cache_switch": False,
        "cache_path": "./cache",
        "max_cache_time": 24.0,
        "cache_send_delay": 1000,
        "change_check": to_int(config.get("changePeriod"), 1),
        "send_data_topic": str(config.get("mqttPub") or "v1/devices/me/telemetry"),
        "rpc_topic": str(config.get("mqttSub") or "v1/devices/me/rpc/request/+"),
        "rpc_response_topic": "",
        "message_tool": "message_platform",
        "rpc_tool": "rpc_tool_platform",
        "devices": [device],
        "storage_max_records_count": 1000,
        "storage_type": "memory",
    }


def empty_project(name: str) -> Dict[str, Any]:
    return {"corridors_eq": [], "corridors_zf": [], "project_name": name}


def credential_value(pattern: Any, fallback_prefix: str, category: str, index: int) -> str:
    text = str(pattern or "").strip()
    if not text:
        text = f"{fallback_prefix}-{{type}}z{{index}}"
    if "{" in text and "}" in text:
        return text.format(type=category, index=index)
    return f"{text.rstrip('-')}-{category}z{index}"


def mqtt_credentials(config: Dict[str, Any], category: str, index: int) -> Dict[str, str]:
    if "identityMap" in config:
        key = category + str(index)
        if key not in config["identityMap"]:
            raise ValueError("实际设备身份映射缺失")
        return dict(config["identityMap"][key])
    fallback_prefix = str(config.get("mqttCredentialPrefix") or "client").rstrip("-")
    return {
        "client_id": credential_value(config.get("mqttClientIdPattern"), fallback_prefix, category, index),
        "user_name": credential_value(config.get("mqttUsernamePattern"), fallback_prefix, category, index),
        "pass_word": credential_value(config.get("mqttPasswordPattern"), fallback_prefix, category, index),
    }


def _row_kind(row: Dict[str, Any]) -> str:
    return str(row.get("type") or "").upper()


def _device_count(row: Dict[str, Any]) -> int:
    return max(0, to_int(row.get("total"), 0))


def _device_points(row: Dict[str, Any]) -> int:
    return max(0, to_int(row.get("points"), 0))


def _allocation_units(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    units: List[Dict[str, Any]] = []
    bms_indices = [i for i, row in enumerate(rows) if _row_kind(row) in BMS_DERIVED_TYPES and _device_count(row) > 0]
    if bms_indices:
        if len(bms_indices) != 3 or len({_device_count(rows[i]) for i in bms_indices}) != 1:
            raise ValueError("BMS、GROUPBMS、CELL 共用采集通道，三类设备数量必须相同")
        count = _device_count(rows[bms_indices[0]])
        weight = sum(_device_points(rows[i]) for i in bms_indices)
        units.append({"key": "BMS_STACK", "indices": bms_indices, "count": count, "weight": weight})

    for i, row in enumerate(rows):
        kind = _row_kind(row)
        if kind in BMS_DERIVED_TYPES:
            continue
        count = _device_count(row)
        weight = _device_points(row)
        if count > 0 and weight > 0:
            units.append({"key": kind, "indices": [i], "count": count, "weight": weight})
    return units


def _apply_unit_projects(rows: List[Dict[str, Any]], units: List[Dict[str, Any]], project_count: int, matrix: List[List[int]]) -> List[Dict[str, Any]]:
    planned = deepcopy(rows)
    for row in planned:
        row["projects"] = [0] * project_count

    for unit_idx, unit in enumerate(units):
        values = [matrix[p][unit_idx] for p in range(project_count)]
        for row_idx in unit["indices"]:
            planned[row_idx]["projects"] = values[:]
            if _row_kind(planned[row_idx]) in BMS_DERIVED_TYPES:
                planned[row_idx]["total"] = unit["count"]
    return planned


def _exact_520w_cl5_allocation(rows: List[Dict[str, Any]], units: List[Dict[str, Any]], key_to_idx: Dict[str, int], max_points: int, project_count: int) -> List[Dict[str, Any]] | None:
    b_unit = units[key_to_idx["BMS_STACK"]]
    p_unit = units[key_to_idx["PCS"]]
    x_unit = units[key_to_idx["BOX"]]
    c_unit = units[key_to_idx["CL"]]
    if (
        project_count != 52
        or max_points != 100000
        or b_unit["count"] != 3130
        or b_unit["weight"] != 1290
        or p_unit["count"] != 3700
        or p_unit["weight"] != 290
        or x_unit["count"] != 260
        or x_unit["weight"] != 130
        or c_unit["count"] != 5
        or c_unit["weight"] != 11100
    ):
        return None

    # This 520w-point profile has no slack: every one of the 52 projects must
    # be exactly 100000 points. The patterns below are integer solutions of
    # 1290*bms_stack + 290*pcs + 11100*cl + 130*box = 100000.
    patterns = [
        {"bms": 3, "pcs": 282, "box": 25, "cl": 1},
        {"bms": 32, "pcs": 198, "box": 10, "cl": 0},
        *[{"bms": 75, "pcs": 0, "box": 25, "cl": 0} for _ in range(9)],
        *[{"bms": 28, "pcs": 182, "box": 0, "cl": 1} for _ in range(4)],
        *[{"bms": 13, "pcs": 287, "box": 0, "cl": 0} for _ in range(5)],
        {"bms": 42, "pcs": 158, "box": 0, "cl": 0},
        *[{"bms": 71, "pcs": 29, "box": 0, "cl": 0} for _ in range(31)],
    ]
    if len(patterns) != project_count:
        return None

    totals = {
        "bms": sum(item["bms"] for item in patterns),
        "pcs": sum(item["pcs"] for item in patterns),
        "box": sum(item["box"] for item in patterns),
        "cl": sum(item["cl"] for item in patterns),
    }
    if totals != {"bms": 3130, "pcs": 3700, "box": 260, "cl": 5}:
        return None
    if any(item["bms"] * 1290 + item["pcs"] * 290 + item["box"] * 130 + item["cl"] * 11100 != max_points for item in patterns):
        return None

    matrix = [[0 for _ in units] for _ in range(project_count)]
    b_idx = key_to_idx["BMS_STACK"]
    p_idx = key_to_idx["PCS"]
    x_idx = key_to_idx["BOX"]
    c_idx = key_to_idx["CL"]
    for project_idx, item in enumerate(patterns):
        matrix[project_idx][b_idx] = item["bms"]
        matrix[project_idx][p_idx] = item["pcs"]
        matrix[project_idx][x_idx] = item["box"]
        matrix[project_idx][c_idx] = item["cl"]
    return _apply_unit_projects(rows, units, project_count, matrix)


def _exact_known_allocation(rows: List[Dict[str, Any]], units: List[Dict[str, Any]], max_points: int, project_count: int) -> List[Dict[str, Any]] | None:
    key_to_idx = {str(unit["key"]): i for i, unit in enumerate(units)}
    required = {"BMS_STACK", "PCS", "BOX", "CL"}
    if not required.issubset(key_to_idx):
        return None

    b_idx = key_to_idx["BMS_STACK"]
    p_idx = key_to_idx["PCS"]
    x_idx = key_to_idx["BOX"]
    c_idx = key_to_idx["CL"]
    b_unit, p_unit, x_unit, c_unit = units[b_idx], units[p_idx], units[x_idx], units[c_idx]
    total_points = sum(unit["count"] * unit["weight"] for unit in units)
    if total_points != project_count * max_points:
        return None
    if any(unit["key"] not in required for unit in units):
        return None

    exact_520w = _exact_520w_cl5_allocation(rows, units, key_to_idx, max_points, project_count)
    if exact_520w is not None:
        return exact_520w

    if c_unit["count"] == 1:
        full_patterns: List[Tuple[int, int, int]] = []
        for b in range(min(b_unit["count"], max_points // b_unit["weight"]) + 1):
            remain_b = max_points - b * b_unit["weight"]
            for p in range(min(p_unit["count"], remain_b // p_unit["weight"]) + 1):
                remain_p = remain_b - p * p_unit["weight"]
                if remain_p % x_unit["weight"] == 0:
                    x = remain_p // x_unit["weight"]
                    if x <= x_unit["count"]:
                        full_patterns.append((b, p, x))

        cl_patterns: List[Tuple[int, int, int]] = []
        cl_target = max_points - c_unit["weight"]
        for b in range(min(b_unit["count"], cl_target // b_unit["weight"]) + 1):
            remain_b = cl_target - b * b_unit["weight"]
            for p in range(min(p_unit["count"], remain_b // p_unit["weight"]) + 1):
                remain_p = remain_b - p * p_unit["weight"]
                if remain_p % x_unit["weight"] == 0:
                    x = remain_p // x_unit["weight"]
                    if x <= x_unit["count"]:
                        cl_patterns.append((b, p, x))

        for cl_b, cl_p, cl_x in cl_patterns:
            need_b = b_unit["count"] - cl_b
            need_x = x_unit["count"] - cl_x
            if need_b < 0 or need_x < 0:
                continue
            states: Dict[Tuple[int, int], None] = {(0, 0): None}
            parents: List[Dict[Tuple[int, int], Tuple[Tuple[int, int], int]]] = []
            for _ in range(project_count - 1):
                nxt: Dict[Tuple[int, int], None] = {}
                parent: Dict[Tuple[int, int], Tuple[Tuple[int, int], int]] = {}
                for state in states:
                    b0, x0 = state
                    for pat_idx, (b, _p, x) in enumerate(full_patterns):
                        nb, nx = b0 + b, x0 + x
                        if nb > need_b or nx > need_x:
                            continue
                        key = (nb, nx)
                        if key not in nxt:
                            nxt[key] = None
                            parent[key] = (state, pat_idx)
                parents.append(parent)
                states = nxt

            final2 = (need_b, need_x)
            if final2 not in states:
                continue

            chosen: List[Tuple[int, int, int]] = []
            key2 = final2
            for level in range(project_count - 2, -1, -1):
                prev, pat_idx = parents[level][key2]
                chosen.append(full_patterns[pat_idx])
                key2 = prev
            chosen.reverse()
            if cl_p + sum(p for _b, p, _x in chosen) != p_unit["count"]:
                continue

            matrix = [[0 for _ in units] for _ in range(project_count)]
            matrix[0][b_idx] = cl_b
            matrix[0][p_idx] = cl_p
            matrix[0][x_idx] = cl_x
            matrix[0][c_idx] = 1
            for project_idx, (b, p, x) in enumerate(chosen, start=1):
                matrix[project_idx][b_idx] = b
                matrix[project_idx][p_idx] = p
                matrix[project_idx][x_idx] = x
            return _apply_unit_projects(rows, units, project_count, matrix)

    patterns: List[Tuple[int, int, int, int]] = []
    for c in range(min(c_unit["count"], max_points // c_unit["weight"]) + 1):
        remain_c = max_points - c * c_unit["weight"]
        for b in range(min(b_unit["count"], remain_c // b_unit["weight"]) + 1):
            remain_b = remain_c - b * b_unit["weight"]
            for p in range(min(p_unit["count"], remain_b // p_unit["weight"]) + 1):
                remain_p = remain_b - p * p_unit["weight"]
                if x_unit["weight"] and remain_p % x_unit["weight"] == 0:
                    x = remain_p // x_unit["weight"]
                    if x <= x_unit["count"]:
                        patterns.append((b, p, x, c))

    target_b, target_x, target_c = b_unit["count"], x_unit["count"], c_unit["count"]
    states: Dict[Tuple[int, int, int], None] = {(0, 0, 0): None}
    parents: List[Dict[Tuple[int, int, int], Tuple[Tuple[int, int, int], int]]] = []

    for _ in range(project_count):
        nxt: Dict[Tuple[int, int, int], None] = {}
        parent: Dict[Tuple[int, int, int], Tuple[Tuple[int, int, int], int]] = {}
        for state in states:
            b0, x0, c0 = state
            for pat_idx, (b, _p, x, c) in enumerate(patterns):
                nb, nx, nc = b0 + b, x0 + x, c0 + c
                if nb > target_b or nx > target_x or nc > target_c:
                    continue
                key = (nb, nx, nc)
                if key not in nxt:
                    nxt[key] = None
                    parent[key] = (state, pat_idx)
        parents.append(parent)
        states = nxt

    final = (target_b, target_x, target_c)
    if final not in states:
        return None

    chosen: List[Tuple[int, int, int, int]] = []
    key = final
    for level in range(project_count - 1, -1, -1):
        prev, pat_idx = parents[level][key]
        chosen.append(patterns[pat_idx])
        key = prev
    chosen.reverse()

    total_p = sum(p for _b, p, _x, _c in chosen)
    if total_p != p_unit["count"]:
        return None

    matrix = [[0 for _ in units] for _ in range(project_count)]
    for project_idx, (b, p, x, c) in enumerate(chosen):
        matrix[project_idx][b_idx] = b
        matrix[project_idx][p_idx] = p
        matrix[project_idx][x_idx] = x
        matrix[project_idx][c_idx] = c
    return _apply_unit_projects(rows, units, project_count, matrix)


def _allocation_totals_match(planned: List[Dict[str, Any]], units: List[Dict[str, Any]], project_count: int) -> bool:
    for unit in units:
        expected = to_int(unit.get("count"), 0)
        row_idx = unit["indices"][0]
        values = planned[row_idx].get("projects") or []
        actual = sum(to_int(values[idx], 0) if idx < len(values) else 0 for idx in range(project_count))
        if actual != expected:
            return False
    return True


def _project_points(planned: List[Dict[str, Any]], project_count: int) -> List[int]:
    return [
        sum(to_int(row.get("projects", [])[idx] if idx < len(row.get("projects", [])) else 0) * _device_points(row) for row in planned)
        for idx in range(project_count)
    ]


def _allocation_is_valid(planned: List[Dict[str, Any]], units: List[Dict[str, Any]], max_points: int, project_count: int) -> bool:
    if not _allocation_totals_match(planned, units, project_count):
        return False
    return all(points <= max_points for points in _project_points(planned, project_count))


def _greedy_allocation(rows: List[Dict[str, Any]], units: List[Dict[str, Any]], max_points: int, project_count: int) -> List[Dict[str, Any]]:
    matrix: List[List[int]] = [[0 for _ in units] for _ in range(project_count)]
    used = [0] * project_count

    for unit_idx, unit in sorted(enumerate(units), key=lambda item: item[1]["weight"], reverse=True):
        left = unit["count"]
        weight = unit["weight"]
        while left > 0:
            candidates = [idx for idx, value in enumerate(used) if value + weight <= max_points]
            if not candidates:
                matrix.append([0 for _ in units])
                used.append(0)
                candidates = [len(matrix) - 1]
            project_idx = min(candidates, key=lambda idx: (used[idx], idx))
            take = 1
            matrix[project_idx][unit_idx] += take
            used[project_idx] += take * weight
            left -= take

    return _apply_unit_projects(rows, units, len(matrix), matrix)


def allocate_type_projects(config: Dict[str, Any], type_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = deepcopy(type_rows or DEFAULT_TYPES)
    for row in rows:
        if 'changeRate' in row:
            row['secondPoints'], row['intervalRules'] = automatic_interval_rules(row)
    max_points = max(1, to_int(config.get("maxProjectPoints"), DEFAULT_CONFIG["maxProjectPoints"]))
    units = _allocation_units(rows)
    total_points = sum(unit["count"] * unit["weight"] for unit in units)
    project_count = max(1, (total_points + max_points - 1) // max_points)

    planned = _exact_known_allocation(rows, units, max_points, project_count)
    if planned is None or not _allocation_is_valid(planned, units, max_points, project_count):
        planned = _greedy_allocation(rows, units, max_points, project_count)
        project_count = max((len(row.get("projects") or []) for row in planned), default=project_count)

    project_points = _project_points(planned, project_count)
    project_devices = [
        sum(to_int(row.get("projects", [])[idx] if idx < len(row.get("projects", [])) else 0) for row in planned)
        for idx in range(project_count)
    ]
    return planned, {
        "projectCount": project_count,
        "totalPoints": total_points,
        "maxProjectPoints": max_points,
        "projectPoints": project_points,
        "projectDevices": project_devices,
    }


def forward_points(refs: Iterable[Dict[str, Any]], gateway: bool = False) -> List[Dict[str, Any]]:
    return [{
        "tag": str(ref["path"]).rsplit(".", 1)[-1] if gateway else str(ref["path"]),
        "name": str(ref["desc"]),
        "flag": True,
        "read_write": 0,
        "valueType": point_value_type(str(ref["tagDesc"])),
        "point_key_in": str(ref["path"]),
        "customize_info": str(ref.get("customize") or ref["subDevice"]),
    } for ref in refs]


def platform_device_name(config: Dict[str, Any], logical_name: str) -> str:
    if logical_name in config.get('deviceNameMap', {}):
        return config['deviceNameMap'][logical_name]
    prefix = str(config.get('platformDevicePrefix', ''))
    name = prefix + logical_name
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', name):
        raise ValueError('平台设备名称仅支持英文字母和数字，且以字母开头')
    return name


def add_forward_channels(project: Dict[str, Any], config: Dict[str, Any], category: str, groups: List[Dict[str, Any]], credential_counters: Dict[str, int]) -> None:
    if not groups:
        return
    max_tags = max(1, to_int(config.get("maxForwardTags"), 6000))
    channel_no = len(project["corridors_zf"]) + 1
    bucket: List[Dict[str, Any]] = []
    bucket_tags = 0

    def flush() -> None:
        nonlocal channel_no, bucket, bucket_tags
        if not bucket:
            return
        first_no = bucket[0]["no"]
        last_no = bucket[-1]["no"]
        alias = f"{category}{first_no}-{last_no}" if first_no != last_no else f"{category}{first_no}"
        credential_counters[category] = credential_counters.get(category, 0) + 1
        credentials = mqtt_credentials(config, category, credential_counters[category])
        name = f"C{channel_no}"
        server = mqtt_server(config, credentials, [], name, alias)
        server.update(message_tool='message_platform', rpc_tool='rpc_tool_platform', status_point_link='')
        device_template = server['devices'][0]
        server['devices'] = [
            {**device_template, 'deviceName': platform_device_name(config, group['refs'][0]['subDevice']),
             'alias_name': '', 'change': True,
             'point_table': forward_points(group['refs'], gateway=True)}
            for group in bucket
        ]
        project["corridors_zf"].append({
            "name": name,
            "alias_name": alias,
            "used": True,
            "type": "mqtt",
            "server": server,
        })
        channel_no += 1
        bucket = []
        bucket_tags = 0

    for group in groups:
        size = len(group["refs"])
        if bucket and bucket_tags + size > max_tags:
            flush()
        bucket.append(group)
        bucket_tags += size
        if bucket_tags >= max_tags:
            flush()
    flush()


def add_forward_channel_for_collect(project: Dict[str, Any], config: Dict[str, Any], corridor: Dict[str, Any], credential_counters: Dict[str, int], alias_override=None, selected_points=None) -> None:
    source_alias = str(corridor.get("alias_name") or "")
    alias = alias_override or source_alias
    category = re.sub(r"\d+$", "", alias).lower() or "dev"
    no_match = re.search(r"(\d+)$", alias)
    credential_index = to_int(no_match.group(1), credential_counters.get(category, 0) + 1) if no_match else credential_counters.get(category, 0) + 1
    device = ((corridor.get("server") or {}).get("devices") or [{}])[0]
    device_alias = str(device.get("alias_name") or "")
    source_channel = str(corridor.get("name") or "")
    refs = [{
        "path": f"{source_channel}.D01.{point['tag']}",
        "desc": " ".join(part for part in (source_alias, device_alias, str(point["name"])) if part),
        "tagDesc": point["name"],
        "subDevice": alias,
        "customize": point["name"],
    } for point in (selected_points if selected_points is not None else list((device.get("point_table") or [])[1:]))]
    credential_counters[category] = max(credential_counters.get(category, 0), credential_index)
    credentials = mqtt_credentials(config, category, credential_index)
    channel_name = f"C{len(project['corridors_zf']) + 1:02d}"
    project["corridors_zf"].append({
        "name": channel_name,
        "alias_name": alias,
        "used": True,
        "type": "mqtt",
        "server": mqtt_server(config, credentials, forward_points(refs), channel_name, alias),
    })


def collect_forward_groups(corridor, config):
    alias = corridor['alias_name']
    points = corridor['server']['devices'][0]['point_table'][1:]
    prefix = config.get('_bmsPrefixes', {}).get('BMS', 'bms')
    match = re.fullmatch(re.escape(prefix) + r'(\d+)', alias)
    sizes = config.get('_bmsSegments')
    if not match or not sizes:
        return [(alias, points)]
    result, offset = [], 0
    for kind in ('BMS', 'GROUPBMS', 'CELL'):
        size = sizes[kind]
        result.append((config['_bmsPrefixes'][kind] + match[1], points[offset:offset+size]))
        offset += size
    if offset != len(points):
        raise ValueError('BMS采集分段点数不一致')
    return result


def add_aggregate_forward_channels(project: Dict[str, Any], config: Dict[str, Any], credential_counters: Dict[str, int]) -> None:
    groups_by_category: Dict[str, List[Dict[str, Any]]] = {}
    for corridor in project["corridors_eq"]:
        device = ((corridor.get("server") or {}).get("devices") or [{}])[0]
        device_alias = str(device.get("alias_name") or "")
        source_channel = str(corridor.get("name") or "")
        for alias, points in collect_forward_groups(corridor, config):
            category = re.sub(r"\d+$", "", alias).lower() or "dev"
            no = int(re.search(r'(\d+)$', alias).group(1))
            refs = [{
                "path": f"{source_channel}.D01.{point['tag']}",
                "desc": " ".join((corridor['alias_name'], device_alias, str(point['name']))),
                "tagDesc": point['name'], "subDevice": alias, "customize": point['name'],
            } for point in points]
            groups_by_category.setdefault(category, []).append({"no": no, "refs": refs})

    for category, groups in groups_by_category.items():
        groups.sort(key=lambda item: item["no"])
        add_forward_channels(project, config, category, groups, credential_counters)


def build_projects(config: Dict[str, Any], type_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    merged_config = {**DEFAULT_CONFIG, **(config or {})}
    active_alarm_rules = alarm_rules(merged_config)
    rows, allocation = allocate_type_projects(merged_config, type_rows or DEFAULT_TYPES)
    project_count = max(1, to_int(allocation.get("projectCount"), 1))
    merged_config["projectCount"] = project_count
    project_prefix = str(merged_config.get("projectPrefix") or "test")
    templates, messages = template_tags(merged_config)

    rows_by_type = {
        str(row.get("type") or "").upper(): row
        for row in rows
    }

    bms_parts = bms_segments(templates.get('BMS') or [])
    bms_full = []
    merged_config['_bmsSegments'] = {}
    merged_config['_bmsPrefixes'] = {}
    for kind in ('BMS', 'GROUPBMS', 'CELL'):
        row = rows_by_type.get(kind, {})
        count = _device_points(row)
        merged_config['_bmsSegments'][kind] = count
        merged_config['_bmsPrefixes'][kind] = row.get('prefix') or kind.lower()
        segment = apply_interval_rule(extend_tags(bms_parts[kind.lower()], kind, count), row)
        if _device_count(row):
            segment = apply_alarm_rule(segment, kind, merged_config)
        for item in segment:
            item['n'] = f'Tag{len(bms_full)+1}'
            bms_full.append(item)
    resolved = {
        "BMS": bms_parts["bms"],
        "GROUPBMS": bms_parts["groupbms"],
        "CELL": bms_parts["cell"],
        "PCS": apply_interval_rule(extend_tags(templates.get("PCS") or [], "PCS", 290), rows_by_type.get("PCS", {})),
        "BOX": apply_interval_rule(extend_tags(templates.get("BOX") or [], "BOX", 130), rows_by_type.get("BOX", {})),
        "CL": apply_interval_rule(extend_tags(templates.get("CL") or [], "CL", 5550), rows_by_type.get("CL", {})),
    }
    projects = [empty_project(f"{project_prefix}{i}") for i in range(1, project_count + 1)]
    next_no: Dict[str, int] = {}
    credential_counters: Dict[str, int] = {}
    summaries = []

    for row in rows:
        kind = str(row.get("type") or "").upper()
        if kind in {'GROUPBMS', 'CELL'}:
            continue
        prefix = str(row.get("prefix") or kind.lower())
        counts = [to_int(x, 0) for x in (row.get("projects") or [])][:project_count]
        counts += [0] * (project_count - len(counts))
        next_no.setdefault(kind, 1)
        for idx, count in enumerate(counts):
            if count <= 0:
                continue
            start_no = next_no[kind]
            end_no = start_no + count - 1
            next_no[kind] = end_no + 1
            project = projects[idx]
            if kind == "BMS":
                tags = bms_full
            elif kind == "GROUPBMS":
                tags = bms_parts["groupbms"]
            elif kind == "CELL":
                tags = bms_parts["cell"]
            else:
                tag_count = max(1, to_int(row.get("points"), 1))
                tags = apply_interval_rule(extend_tags(templates.get(kind) or [], kind, tag_count), row)
                tags = apply_alarm_rule(tags, kind, merged_config)
            for no in range(start_no, end_no + 1):
                channel_name = f"C{len(project['corridors_eq']) + 1}"
                alias = f"{prefix}{no}"
                points = [device_status_point()] + [collect_point(t) for t in tags]
                project["corridors_eq"].append({
                    "name": channel_name,
                    "alias_name": alias,
                    "used": True,
                    "type": "simulate_enhanced",
                    "class_name": "SimulateEnhancedConnector",
                    "server": {
                        "disconnected_option": 0,
                        "devices": [{
                            "refresh_interval": 1.0,
                            "point_table": points,
                            "deviceName": "D01",
                            "alias_name": "设备01",
                            "used": True,
                        }],
                    },
                })
                summaries.append({"project": project["project_name"], "type": kind, "no": no, "points": len(tags)})

    forward_mode = str(merged_config.get("forwardMode") or "aggregate").lower()
    for project in projects:
        if forward_mode == "direct":
            for corridor in project["corridors_eq"]:
                for alias, points in collect_forward_groups(corridor, merged_config):
                    add_forward_channel_for_collect(project, merged_config, corridor, credential_counters, alias, points)
        else:
            add_aggregate_forward_channels(project, merged_config, credential_counters)

    for rule in active_alarm_rules:
        total = _device_count(rows_by_type.get(rule['type'], {}))
        messages.append(f"{rule['type']}告警规则：{rule['description']}，类型编号 {rule['generatorType']}，周期 {rule['interval']} 毫秒，生效设备 {total} 个")
    summary = {
        "messages": messages,
        "forwardMode": forward_mode,
        "projectCount": len(projects),
        "allocation": allocation,
        "projects": [{
            "name": p["project_name"],
            "collectChannels": len(p["corridors_eq"]),
            "forwardChannels": len(p["corridors_zf"]),
            "collectPoints": sum(max(0, len((((c.get("server") or {}).get("devices") or [{}])[0].get("point_table") or [])) - 1) for c in p["corridors_eq"]),
            "forwardPoints": sum(len(((((c.get("server") or {}).get("devices") or [{}])[0]).get("point_table") or [])) for c in p["corridors_zf"]),
        } for p in projects],
    }
    return projects, summary


def write_zip(projects: List[Dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for project in projects:
            text = json.dumps(project, ensure_ascii=False, indent=2)
            zf.writestr(f"{project['project_name']}/tb_gateway.json", text)


def generate_package(config: Dict[str, Any] | None = None, type_rows: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    projects, summary = build_projects(config or {}, type_rows or DEFAULT_TYPES)
    output = BASE / "data" / "gateway-projects.zip"
    write_zip(projects, output)
    summary.update({"ok": True, "downloadName": "gateway-projects.zip", "downloadPath": str(output), "bytes": output.stat().st_size})
    return summary


def plan_allocation(config: Dict[str, Any] | None = None, type_rows: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    merged_config = {**DEFAULT_CONFIG, **(config or {})}
    rows, allocation = allocate_type_projects(merged_config, type_rows or DEFAULT_TYPES)
    return {"ok": True, "config": {"projectCount": allocation["projectCount"]}, "types": rows, "allocation": allocation}
