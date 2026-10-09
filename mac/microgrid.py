"""Microgrid profile: independent device taxonomy and explicitly synthetic points."""
import json
from copy import deepcopy
from collections import Counter
from hashlib import sha256
from pathlib import Path
from gateway_engine import workbench as w
from gateway_engine import gateway_generator as g

KINDS = ('PCS', 'ESS', 'INV', 'BOX')
PRESET = dict(zip(KINDS, ((300,356,54),(302,4597,690),(76,5621,843),(264,296,44))))
VERSION = 'microgrid-2'
POINT_TEMPLATE = Path(__file__).resolve().parent / 'templates' / 'microgrid_points.json'


def load_point_template(expected_hash=None):
    blob = POINT_TEMPLATE.read_bytes()
    fingerprint = sha256(blob).hexdigest()
    if expected_hash and fingerprint != expected_hash:
        raise ValueError('微电网测点模板已变化，请重新计算')
    data = json.loads(blob)
    if data.get('generatorType') != 0:
        raise ValueError('微电网测点生成类型必须为递增1')
    templates = data['templates']
    for kind in KINDS:
        points = templates[kind]
        if len({point['tag'] for point in points}) != len(points) or len({point['name'] for point in points}) != len(points):
            raise ValueError(f'{kind}测点模板存在重复点名或描述')
    return templates, fingerprint

def station_plan(rows, count):
    if count in (None, ''):
        return []
    count = w.integer(count, '电站数量', 1, 10000)
    result = []
    for i in range(count):
        counts = {r['type']: r['total']//count + (i < r['total']%count) for r in rows}
        result.append({'station': i+1, 'counts': counts, 'devices': sum(counts.values()),
                       'points': sum(counts[r['type']]*(r['points'] or 0) for r in rows)})
    return result

def prepare(payload):
    config = deepcopy(payload['config']); rows = deepcopy(payload['types'])
    if {r['type'] for r in rows} != set(KINDS) or len(rows) != 4:
        raise ValueError('微电网只支持 PCS、ESS、INV、BOX 四类设备')
    if config['forwardMode'] != 'direct':
        raise ValueError('微电网当前使用单设备直连转发')
    if not sum(r['total'] for r in rows):
        raise ValueError('设备总数必须大于0')
    config['binding'] = 'independent'
    templates, template_hash = load_point_template(config.get('microgridTemplateSha256'))
    config['microgridTemplateSha256'] = template_hash
    resolved, reports = {}, []
    for row in rows:
        n = w.integer(row['points'], '单设备点数', 1, 20000000)
        w.integer(row['total'], '设备数量', 0, 100000)
        periods = w.rules(row)
        source = templates[row['type']]
        missing = max(0, n - len(source))
        if missing and not config.get('allowFill'):
            raise ValueError(f"{row['type']}模板只有{len(source)}点，缺少{missing}点；请补充模板或允许占位补点")
        tags = g.extend_tags([g.normalize_tag(point, i, row['type']) for i, point in enumerate(source[:n], 1)], row['type'], n)
        offset = 0
        for interval, count in periods:
            end = n if count is None else offset+count
            for tag in tags[offset:end]:
                tag['p'] = interval
                tag['s0'] = '0'  # 递增1；采集周期仍由设备配置决定。
            offset = end
        resolved[row['type']] = tags
        reports.append({'type':row['type'], 'templatePoints':len(source), 'realPoints':0,
                        'fillPoints':missing, 'periods':dict(Counter(int(t['p']) for t in tags)),
                        'source':'tb_gateway.json 模拟测点模板',
                        'sample':[{'tag':t['n'], 'description':t['d'], 'generator':0,
                                   'interval':int(t['p'])} for t in tags[:100]]})
    planned, projects, summary = w.allocate(config, rows)
    snapshot = {'config':config, 'types':rows}
    plan = {'snapshot':snapshot, 'projects':projects, 'summary':summary,
            'templates':reports,
            'sources':[{'key':'microgridPoints','name':POINT_TEMPLATE.name,'sha256':template_hash}],
            'planId':w.digest({'snapshot':snapshot,'version':VERSION})}
    return plan, planned, resolved


# These routines operate on the supplied rows; no storage taxonomy is mutated.
iter_projects = w.iter_projects
validate_project = w.validate_project
