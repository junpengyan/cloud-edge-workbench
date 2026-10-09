from __future__ import annotations

import csv
from io import StringIO

from app.models.entities import TemplateItem


def parse_template(template: str) -> list[TemplateItem]:
    """Parse a multiline template in the format: name,count[,type]."""

    normalized = template.replace("\ufeff", "").strip()
    if not normalized:
        return []

    rows: list[TemplateItem] = []
    reader = csv.reader(StringIO(normalized))
    for line_number, fields in enumerate(reader, start=1):
        if not fields or all(not field.strip() for field in fields):
            continue
        if fields[0].strip().startswith("#"):
            continue
        if len(fields) not in {2, 3}:
            raise ValueError(f"template line {line_number} must contain 2 or 3 columns")

        name, count_text = (field.strip() for field in fields[:2])
        type_name = fields[2].strip() if len(fields) == 3 else "default"
        if not name:
            raise ValueError(f"template line {line_number} has an empty name")
        if not type_name:
            type_name = "default"
        try:
            count = int(count_text)
        except ValueError as exc:
            raise ValueError(f"template line {line_number} count must be an integer") from exc
        if count < 1:
            raise ValueError(f"template line {line_number} count must be >= 1")

        rows.append(TemplateItem(name=name, count=count, type=type_name))
    return rows


def count_template_items(template: str) -> int:
    return sum(item.count for item in parse_template(template))


def parse_name_list(names: str) -> list[str]:
    normalized = names.replace("\ufeff", "").replace("，", ",").strip()
    if not normalized:
        return []

    parsed: list[str] = []
    reader = csv.reader(StringIO(normalized))
    for fields in reader:
        for field in fields:
            name = field.strip()
            if name and not name.startswith("#"):
                parsed.append(name)
    return parsed
