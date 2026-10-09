from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from io import StringIO

from app.models.entities import EntityRecord


AUTO_COUNT = -1


@dataclass(frozen=True)
class RelationRule:
    from_name: str
    from_entity_type: str
    to_name: str
    to_entity_type: str
    to_count_per_from: int
    relation_type: str
    additional_info: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedRelation:
    from_entity: EntityRecord
    to_entity: EntityRecord
    relation_type: str
    additional_info: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RelationResolution:
    relations: list[ResolvedRelation]
    rule_count: int
    skipped_count: int


_TEMPLATE_KEY_ALIASES = {
    "company": "company",
    "公司": "company",
    "station": "station",
    "电站": "station",
    "powerstation": "station",
    "unit": "unit",
    "储能单元": "unit",
    "container": "unit",
    "pcs": "pcs",
    "bms": "bms",
    "groupbms": "groupbms",
    "cell": "cell",
}


def parse_relation_rules(rules_text: str) -> list[RelationRule]:
    normalized = rules_text.replace("\ufeff", "").strip()
    if not normalized:
        return []

    rules: list[RelationRule] = []
    reader = csv.reader(StringIO(normalized))
    for line_number, fields in enumerate(reader, start=1):
        if not fields or all(not field.strip() for field in fields):
            continue
        if fields[0].strip().startswith("#"):
            continue
        if len(fields) not in {4, 5, 6, 7}:
            raise ValueError(f"relation rule line {line_number} must contain 4, 5, 6 or 7 columns")
        from_name, from_entity_type, to_name, to_entity_type = (field.strip() for field in fields[:4])
        count_text = fields[4].strip() if len(fields) >= 5 else "auto"
        relation_type = fields[5].strip() if len(fields) >= 6 and fields[5].strip() else "Contains"
        additional_info = _parse_additional_info(fields[6].strip(), line_number) if len(fields) == 7 else {}
        if not from_name or not to_name:
            raise ValueError(f"relation rule line {line_number} has an empty entity name")
        if from_entity_type not in {"ASSET", "DEVICE"} or to_entity_type not in {"ASSET", "DEVICE"}:
            raise ValueError(f"relation rule line {line_number} entity type must be ASSET or DEVICE")
        count = _parse_count(count_text, line_number)
        if count != AUTO_COUNT and count < 1:
            raise ValueError(f"relation rule line {line_number} count must be >= 1, blank or auto")
        rules.append(
            RelationRule(
                from_name=from_name,
                from_entity_type=from_entity_type,
                to_name=to_name,
                to_entity_type=to_entity_type,
                to_count_per_from=count,
                relation_type=relation_type,
                additional_info=additional_info,
            )
        )
    return rules


def resolve_relation_rules(
    rules_text: str,
    *,
    assets: list[EntityRecord],
    devices: list[EntityRecord],
) -> RelationResolution:
    rules = parse_relation_rules(rules_text)
    created_entities = _group_created_entities(assets, devices)
    relations: list[ResolvedRelation] = []
    skipped_count = 0
    seen: set[tuple[str, str, str]] = set()
    for rule in rules:
        from_entities = created_entities.get((rule.from_entity_type, _canonical_template_key(rule.from_name)), [])
        to_entities = created_entities.get((rule.to_entity_type, _canonical_template_key(rule.to_name)), [])
        allocation_counts = _allocation_counts(len(from_entities), len(to_entities), rule.to_count_per_from)
        to_cursor = 0
        for from_entity, to_count in zip(from_entities, allocation_counts):
            for _ in range(to_count):
                if to_cursor >= len(to_entities):
                    skipped_count += 1
                    continue
                to_entity = to_entities[to_cursor]
                to_cursor += 1
                relation_key = (from_entity.id, to_entity.id, rule.relation_type)
                if relation_key in seen:
                    continue
                seen.add(relation_key)
                relations.append(
                    ResolvedRelation(
                        from_entity=from_entity,
                        to_entity=to_entity,
                        relation_type=rule.relation_type,
                        additional_info=rule.additional_info,
                    )
                )
    return RelationResolution(relations=relations, rule_count=len(rules), skipped_count=skipped_count)


def _parse_additional_info(value: str, line_number: int) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"relation rule line {line_number} additionalInfo must be JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"relation rule line {line_number} additionalInfo must be a JSON object")
    return parsed


def _parse_count(value: str, line_number: int) -> int:
    if not value or value.lower() == "auto":
        return AUTO_COUNT
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"relation rule line {line_number} count must be an integer, blank or auto") from exc


def _allocation_counts(parent_count: int, child_count: int, configured_count: int) -> list[int]:
    if parent_count <= 0:
        return []
    if configured_count != AUTO_COUNT:
        return [configured_count] * parent_count
    base, remainder = divmod(child_count, parent_count)
    return [base + (1 if index < remainder else 0) for index in range(parent_count)]


def _group_created_entities(
    assets: list[EntityRecord],
    devices: list[EntityRecord],
) -> dict[tuple[str, str], list[EntityRecord]]:
    groups: dict[tuple[str, str], list[EntityRecord]] = {}
    for entity in [*assets, *devices]:
        template_name = _note_value(entity.notes, "template")
        template_key = _canonical_template_key(template_name)
        groups.setdefault((entity.entity_type, template_key), []).append(entity)
        template_index = _note_value(entity.notes, "templateIndex")
        if template_index:
            groups.setdefault((entity.entity_type, f"#{template_index}"), []).append(entity)
    return groups


def _note_value(notes: str, wanted_key: str) -> str:
    for part in notes.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == wanted_key:
            return value.strip()
    return ""


def _canonical_template_key(template_name: str) -> str:
    normalized = template_name.strip().replace("-", "").replace("_", "").replace(" ", "").lower()
    row_match = re.fullmatch(r"(?:a|asset|资产|d|device|设备)(\d+)", normalized)
    if row_match:
        return f"#{row_match.group(1)}"
    return _TEMPLATE_KEY_ALIASES.get(normalized, normalized)
