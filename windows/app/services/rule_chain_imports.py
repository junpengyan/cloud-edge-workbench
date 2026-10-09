from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO


@dataclass(frozen=True)
class RuleChainImport:
    name: str
    type: str
    template: str
    count: int = 1


def parse_rule_chain_imports(imports_text: str) -> list[RuleChainImport]:
    normalized = imports_text.replace("\ufeff", "").strip()
    if not normalized:
        return []

    items: list[RuleChainImport] = []
    reader = csv.reader(StringIO(normalized))
    for line_number, fields in enumerate(reader, start=1):
        if not fields or all(not field.strip() for field in fields):
            continue
        if fields[0].strip().startswith("#"):
            continue
        if len(fields) not in {3, 4}:
            raise ValueError(f"rule chain import line {line_number} must contain 3 or 4 columns")
        name, type_name, template_name = (field.strip() for field in fields[:3])
        count_text = fields[3].strip() if len(fields) == 4 else "1"
        if not name:
            raise ValueError(f"rule chain import line {line_number} has an empty name")
        type_name = type_name.upper()
        if type_name not in {"CORE", "EDGE"}:
            raise ValueError(f"rule chain import line {line_number} type must be CORE or EDGE")
        if not template_name:
            raise ValueError(f"rule chain import line {line_number} has an empty template")
        try:
            count = int(count_text)
        except ValueError as exc:
            raise ValueError(f"rule chain import line {line_number} count must be an integer") from exc
        if count < 1:
            raise ValueError(f"rule chain import line {line_number} count must be >= 1")
        items.append(RuleChainImport(name=name, type=type_name, template=template_name, count=count))
    return items
