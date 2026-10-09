from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List


def decode_bytes(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def attrs(elem: ET.Element) -> Dict[str, str]:
    return dict(elem.attrib)


def type_from_desc(desc: str) -> str:
    if re.match(r"^bms", desc or "", re.I):
        return "BMS"
    if re.match(r"^pcs", desc or "", re.I):
        return "PCS"
    if re.match(r"^box", desc or "", re.I):
        return "BOX"
    if re.match(r"^cl", desc or "", re.I) or "策略" in (desc or ""):
        return "CL"
    return ""


def type_from_tag_descriptions(descs: List[str]) -> str:
    if any(re.match(r"^(BMS_|GroupBMS_|Cell_)", d or "", re.I) for d in descs):
        return "BMS"
    if any(re.match(r"^PCS_", d or "", re.I) for d in descs):
        return "PCS"
    if any(re.match(r"^BOX_", d or "", re.I) for d in descs):
        return "BOX"
    if any(re.match(r"^CL_", d or "", re.I) or re.search(r"_ep$", d or "", re.I) for d in descs):
        return "CL"
    return ""



def infer_device_type(desc: str, tag_descs: List[str]) -> str:
    """Infer device type from channel/device description first, then tag descriptions."""
    t = type_from_desc(desc or "")
    if t:
        return t
    return type_from_tag_descriptions(tag_descs)


def split_prefix_number(value: str, fallback_type: str) -> tuple[str, int | None]:
    """Split names like bms62 / PCS_001 into prefix and number."""
    text = str(value or "").strip()
    m = re.match(r"^(.*?)(\d+)\s*$", text)
    if m:
        prefix = m.group(1).strip() or fallback_type.lower()
        return prefix, int(m.group(2))
    return (fallback_type.lower(), None)


def compress_numbers_to_rows(device_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert discovered device instances into rows for the device table.

    The UI table supports prefix + startNo + count. To avoid hiding non-contiguous
    devices, each contiguous numeric range is returned as a separate row. Devices
    without numbers are returned as count=1 rows.
    """
    buckets: Dict[tuple[str, str, int], List[int | None]] = {}
    for item in device_items:
        t = str(item.get("type") or "").upper()
        prefix = str(item.get("prefix") or t.lower())
        points = int(item.get("points") or 0)
        no = item.get("no")
        buckets.setdefault((t, prefix, points), []).append(no)

    rows: List[Dict[str, Any]] = []
    for (t, prefix, points), numbers in buckets.items():
        numeric = sorted({int(n) for n in numbers if isinstance(n, int)})
        none_count = sum(1 for n in numbers if not isinstance(n, int))
        if numeric:
            start = prev = numeric[0]
            for n in numeric[1:]:
                if n == prev + 1:
                    prev = n
                    continue
                rows.append({"type": t, "prefix": prefix, "startNo": start, "count": prev - start + 1, "points": points})
                start = prev = n
            rows.append({"type": t, "prefix": prefix, "startNo": start, "count": prev - start + 1, "points": points})
        for _ in range(none_count):
            rows.append({"type": t, "prefix": prefix, "startNo": 1, "count": 1, "points": points})

    def sort_key(r: Dict[str, Any]):
        order = {"BMS": 1, "PCS": 2, "BOX": 3, "CL": 4, "CELL": 5, "GROUPBMS": 6}
        return (order.get(str(r.get("type")), 99), str(r.get("prefix")), int(r.get("startNo") or 0), int(r.get("points") or 0))
    return sorted(rows, key=sort_key)



def bms_segment_counts(tags: List[Dict[str, str]]) -> Dict[str, int]:
    counts = {"bms": 0, "groupbms": 0, "cell": 0}
    for tag in tags:
        d = tag.get("d", "")
        if re.match(r"^GroupBMS_", d or "", re.I):
            counts["groupbms"] += 1
        elif re.match(r"^Cell_", d or "", re.I):
            counts["cell"] += 1
        else:
            counts["bms"] += 1
    return counts


def add_bms_segment_rows(rows: List[Dict[str, Any]], templates: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Keep the BMS template as one IO template, but expose BMS/GROUPBMS/CELL
    segment counts in the device table for DS grouping configuration.
    """
    bms_tpl = templates.get("BMS")
    if not bms_tpl:
        return rows
    seg = bms_segment_counts(bms_tpl.get("tags") or [])
    if not (seg["cell"] or seg["groupbms"]):
        return rows

    next_rows: List[Dict[str, Any]] = []
    bms_refs: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("type", "")).upper() == "BMS":
            new_row = dict(row)
            # UI shows only the BMS_ segment count. Generation still uses full BMS template.
            new_row["points"] = seg["bms"] or bms_tpl.get("tagCount") or len(bms_tpl.get("tags") or [])
            next_rows.append(new_row)
            bms_refs.append(new_row)
        else:
            next_rows.append(row)

    if not bms_refs:
        bms_refs = [{"type": "BMS", "prefix": "bms", "startNo": 1, "count": 1, "points": seg["bms"]}]
        next_rows.extend(bms_refs)

    def exists(t: str, start_no: int, count: int) -> bool:
        return any(str(r.get("type", "")).upper() == t and int(r.get("startNo") or 1) == start_no and int(r.get("count") or 0) == count for r in next_rows)

    for bms in bms_refs:
        start_no = int(bms.get("startNo") or 1)
        count = int(bms.get("count") or 1)
        if seg["groupbms"] > 0 and not exists("GROUPBMS", start_no, count):
            next_rows.append({"type": "GROUPBMS", "prefix": "groupbms", "startNo": start_no, "count": count, "points": seg["groupbms"]})
        if seg["cell"] > 0 and not exists("CELL", start_no, count):
            next_rows.append({"type": "CELL", "prefix": "cell", "startNo": start_no, "count": count, "points": seg["cell"]})

    def sort_key(r: Dict[str, Any]):
        order = {"BMS": 1, "GROUPBMS": 2, "CELL": 3, "PCS": 4, "BOX": 5, "CL": 6}
        return (order.get(str(r.get("type", "")).upper(), 99), str(r.get("prefix")), int(r.get("startNo") or 0))
    return sorted(next_rows, key=sort_key)

def parse_xml_template(data: bytes, filename: str) -> Dict[str, Any]:
    text = decode_bytes(data)
    root = ET.fromstring(text)
    templates: Dict[str, Any] = {}
    discovered_devices: List[Dict[str, Any]] = []

    io_node = root.find("io") if root.tag == "root" else root.find("./io")
    if root.tag == "io":
        io_node = root
    if io_node is None:
        raise ValueError("XML 中未找到 <io> 节点。")

    for channel in list(io_node.findall("channel")):
        cnfg = channel.find("cnfg")
        channel_desc = cnfg.get("desc", "") if cnfg is not None else ""
        channel_name = cnfg.get("name", filename) if cnfg is not None else filename
        device_nodes = list(channel.findall("./devices/device"))
        if not device_nodes:
            continue

        for device in device_nodes:
            dcnfg = device.find("cnfg")
            device_desc = dcnfg.get("desc", "") if dcnfg is not None else ""
            tags_node = device.find("tags")
            tags = [attrs(x) for x in list(tags_node.findall("t"))] if tags_node is not None else []
            if not tags:
                continue

            tag_descs = [x.get("d", "") for x in tags]
            desc_for_type = device_desc or channel_desc
            t = infer_device_type(desc_for_type, tag_descs)
            if not t:
                continue

            # Template: keep the first occurrence for each type as the tag template.
            if t not in templates:
                templates[t] = {
                    "type": t,
                    "sourceChannel": channel_name,
                    "sourceDesc": channel_desc or device_desc or filename,
                    "deviceAttrs": attrs(dcnfg) if dcnfg is not None else {},
                    "tags": tags,
                    "tagCount": len(tags),
                    "firstTag": tags[0].get("d") or tags[0].get("n") or "",
                    "lastTag": tags[-1].get("d") or tags[-1].get("n") or "",
                }

            # Device list: collect every device instance found in the template XML.
            source_desc = device_desc or channel_desc or t.lower()
            prefix, no = split_prefix_number(source_desc, t)
            discovered_devices.append({"type": t, "prefix": prefix, "no": no, "points": len(tags)})

    if not templates:
        raise ValueError("未能从 XML 的 <io> 中识别 BMS/PCS/BOX/CL 模板，请检查通道/设备描述或点描述。")

    return {"templates": templates, "devices": add_bms_segment_rows(compress_numbers_to_rows(discovered_devices), templates)}


def parse_csv_template(data: bytes, filename: str) -> Dict[str, Any]:
    text = decode_bytes(data)
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("CSV 内容为空。")
    headers = [h.strip() for h in rows[0]]
    def idx(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError:
            return -1
    tag_i = idx("点名(英文)")
    desc_i = idx("点描述(中文)")
    period_i = idx("采集周期")
    unit_i = idx("单位")
    s0_i = idx("s0")
    s1_i = idx("s1")
    s2_i = idx("s2")
    if min(tag_i, desc_i, period_i, s0_i, s1_i, s2_i) < 0:
        raise ValueError("CSV 模板缺少必要列：点名(英文)、点描述(中文)、采集周期、s0、s1、s2。")
    data_rows = rows[1:]
    descs = [(r[desc_i] if len(r) > desc_i else "") for r in data_rows]
    t = type_from_tag_descriptions(descs)
    if not t:
        raise ValueError("无法从 CSV 点描述中识别设备类型，需要 BMS_/GroupBMS_/Cell_、PCS_ 或 BOX_ 前缀。")
    tags = []
    for i, row in enumerate(data_rows, start=1):
        def cell(j: int, default: str = "") -> str:
            return row[j].strip() if j >= 0 and len(row) > j else default
        tags.append({
            "v": "1", "n": cell(tag_i, f"Tag{i}"), "d": cell(desc_i, f"{t}_Tag{i}"), "r": "0",
            "p": cell(period_i, "1000"), "u": cell(unit_i, ""), "s0": cell(s0_i, ""), "s1": cell(s1_i, ""), "s2": cell(s2_i, ""),
        })
    template = {
        t: {
            "type": t,
            "sourceChannel": re.sub(r"\.[^.]+$", "", filename),
            "sourceDesc": f"{t} CSV",
            "deviceAttrs": {},
            "tags": tags,
            "tagCount": len(tags),
            "firstTag": tags[0].get("d", "") if tags else "",
            "lastTag": tags[-1].get("d", "") if tags else "",
        }
    }
    # CSV only describes point templates. Use count=1 so the user can adjust数量 after导入。
    return {"templates": template, "devices": add_bms_segment_rows([{"type": t, "prefix": t.lower(), "startNo": 1, "count": 1, "points": len(tags)}], template)}


def parse_template(data: bytes, filename: str) -> Dict[str, Any]:
    if filename.lower().endswith(".csv"):
        return parse_csv_template(data, filename)
    return parse_xml_template(data, filename)
