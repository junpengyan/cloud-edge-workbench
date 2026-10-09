"""Validated plans and immutable, independently checked generation runs."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json
import re
import zipfile
from . import gateway_generator as g

VERSION = '2.3.0'
DATA = g.BASE.parent / 'data' / 'gateway'
DEFAULTS = {**deepcopy(g.DEFAULT_CONFIG), 'binding': 'stack', 'strategy': 'balanced', 'allowFill': False}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def integer(value, label, low=0, high=100000000):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError(f'{label}必须是整数')
    value = int(value)
    if not low <= value <= high:
        raise ValueError(f'{label}必须在 {low} 至 {high} 之间')
    return value


def normalize(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('config', {}), dict):
        raise ValueError('方案格式无效')
    config = {**deepcopy(DEFAULTS), **payload.get('config', {})}
    config['platformDevicePrefix'] = str(config.get('platformDevicePrefix', ''))
    if not re.fullmatch(r'[A-Za-z0-9]*', config['platformDevicePrefix']):
        raise ValueError('平台设备名前缀只能包含英文字母和数字')
    if 'overrides' in config and 'alarmRules' not in payload.get('config', {}):
        config['alarmRules'] = [{**r, 'description': r['name'], 'generatorType': 4,
                                'var1': '', 'var2': '0'} for r in config['overrides']]
    config.pop('overrides', None)
    g.alarm_rules(config)
    rows = deepcopy(payload.get('types', g.DEFAULT_TYPES))
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        raise ValueError('至少配置一种设备')
    config['maxProjectPoints'] = integer(config['maxProjectPoints'], '工程容量', 1)
    for key, low, high in [('mqttPort', 1, 65535), ('mqttQos', 0, 2), ('changePeriod', 0, 100000000), ('pubTimer', 0, 100000000)]:
        config[key] = integer(config[key], key, low, high)
    if config['binding'] != 'stack':
        raise ValueError('旧方案使用独立采集；请将 BMS、GROUPBMS、CELL 改为合并采集后重新计算')
    if config['strategy'] not in ('balanced', 'compact') or not isinstance(config['allowFill'], bool):
        raise ValueError('分配规则或补点设置无效')
    if config['forwardMode'] not in ('direct', 'aggregate'):
        raise ValueError('转发方式无效')
    config['maxForwardTags'] = integer(config['maxForwardTags'], '汇总通道点数上限', 1)
    if not re.fullmatch(r'[\w-]{1,40}', config['projectPrefix']):
        raise ValueError('工程前缀仅支持文字、数字、下划线和短横线，最多40字')
    for key in ('mqttHost', 'mqttPub', 'mqttClientIdPattern', 'mqttUsernamePattern', 'mqttPasswordPattern'):
        if not str(config[key]).strip():
            raise ValueError(f'{key}不能为空')
    kinds, prefixes = set(), set()
    for row in rows:
        kind = row['type'] = str(row.get('type', '')).upper()
        if kind not in {r['type'] for r in g.DEFAULT_TYPES} or kind in kinds:
            raise ValueError('设备类型无效或重复')
        kinds.add(kind)
        prefix = str(row.get('prefix', ''))
        if not re.fullmatch(r'[A-Za-z][A-Za-z_]*', prefix) or prefix.lower() in prefixes:
            raise ValueError('设备前缀必须唯一，仅支持字母和下划线')
        prefixes.add(prefix.lower())
        if config['forwardMode'] == 'aggregate':
            g.platform_device_name(config, prefix + '1')
        row['total'] = integer(row.get('total'), f'{kind}数量')
        row['points'] = integer(row.get('points'), f'{kind}点数', 1)
        row.pop('projects', None)
        if 'changeRate' in row:
            row['secondPoints'], row['intervalRules'] = g.automatic_interval_rules(row)
        if row['total'] and row['points'] > config['maxProjectPoints']:
            raise ValueError(f'{kind}单设备点数超过工程容量，设备不能拆分')
        rules(row)
        try:
            if row['total']:
                g.mqtt_credentials(config, prefix.lower(), 1)
        except (KeyError, ValueError, IndexError):
            raise ValueError('凭证规则只支持 {type} 和 {index}')
    if not sum(r['total'] for r in rows):
        raise ValueError('设备总数必须大于0')
    if sum(r['total'] for r in rows) > 100000 or sum(r['total']*r['points'] for r in rows) > 20000000:
        raise ValueError('本地版单次上限为10万逻辑设备、2000万业务点，请拆分方案')
    return config, rows


def rules(row):
    parts = re.split(r'[,，;；]+', str(row.get('intervalRules', '')))
    result, count, rest = [], 0, False
    for part in parts:
        match = re.fullmatch(r'\s*(\d+)\s*:\s*(\d+|\*)\s*', part)
        if not match or rest:
            raise ValueError(f"{row['type']}周期规则无效；剩余点必须放在最后")
        interval = integer(match[1], '采集周期(毫秒)', 1)
        number = None if match[2] == '*' else integer(match[2], '周期点数')
        result.append((str(interval), number))
        rest = number is None
        count += number or 0
    if count > row['points'] or (not rest and count != row['points']):
        raise ValueError(f"{row['type']}周期分段点数与单设备点数不符")
    return result


def allocate(config, rows):
    units = []
    bound = [i for i, r in enumerate(rows) if r['type'] in g.BMS_DERIVED_TYPES]
    if config['binding'] == 'stack' and bound:
        counts = {rows[i]['total'] for i in bound}
        if len(bound) != 3 or len(counts) != 1:
            raise ValueError('BMS、GROUPBMS、CELL 共用采集通道，三类数量必须相同')
        if rows[bound[0]]['total']:
            units.append({'key': 'BMS_STACK', 'indices': bound, 'count': rows[bound[0]]['total'], 'weight': sum(rows[i]['points'] for i in bound)})
    for i, row in enumerate(rows):
        if row['total'] and not (config['binding'] == 'stack' and i in bound):
            units.append({'key': row['type'], 'indices': [i], 'count': row['total'], 'weight': row['points']})
    capacity = config['maxProjectPoints']
    if any(u['weight'] > capacity for u in units):
        raise ValueError('绑定设备组合点数超过单工程容量')
    total = sum(r['total'] * r['points'] for r in rows)
    lower = (total + capacity - 1) // capacity
    if lower > 1000:
        raise ValueError('单次工程数超过1000，请提高工程容量或拆分方案')
    lookup = {u['key']: i for i, u in enumerate(units)}
    planned = None
    if set(lookup) == {'BMS_STACK', 'PCS', 'BOX', 'CL'}:
        planned = g._exact_520w_cl5_allocation(rows, units, lookup, capacity, lower)
    method = '已验证的520万点整数分配' if planned else '启发式分配（不保证工程数最优）'
    if planned is None:
        matrix, used = [], []
        if config['strategy'] == 'balanced':
            matrix, used = [[0]*len(units) for _ in range(lower)], [0]*lower
        for j, unit in sorted(enumerate(units), key=lambda item: -item[1]['weight']):
            for _ in range(unit['count']):
                candidates = [i for i, n in enumerate(used) if n + unit['weight'] <= capacity]
                if not candidates:
                    if len(used) >= 1000:
                        raise ValueError('实际分配超过1000个工程，请拆分方案')
                    candidates = [len(used)]
                    matrix.append([0]*len(units))
                    used.append(0)
                i = min(candidates, key=lambda i: (used[i] if config['strategy']=='balanced' else -used[i], i))
                matrix[i][j] += 1
                used[i] += unit['weight']
        matrix = [v for v in matrix if any(v)]
        planned = g._apply_unit_projects(rows, units, len(matrix), matrix)
    projects = []
    for i in range(len(planned[0]['projects'])):
        counts = {r['type']: r['projects'][i] for r in planned}
        points = sum(r['projects'][i]*r['points'] for r in planned)
        devices = sum(counts.values())
        physical = devices - counts.get('GROUPBMS', 0) - counts.get('CELL', 0)
        if points > capacity:
            raise ValueError('分配结果超过工程容量')
        forward_channels = devices
        if config['forwardMode'] == 'aggregate':
            forward_channels = sum((r['projects'][i] + max(1, config['maxForwardTags']//r['points']) - 1)
                                   // max(1, config['maxForwardTags']//r['points']) for r in planned)
        projects.append({'name': f"{config['projectPrefix']}{i+1}", 'counts': counts,
                         'devices': devices, 'collectChannels': physical, 'forwardChannels': forward_channels,
                         'points': points, 'remaining': capacity-points, 'systemPoints': physical})
    for before, after in zip(rows, planned):
        if before['total'] != sum(after['projects']):
            raise ValueError('分配数量不守恒')
    return planned, projects, {'method': method, 'lowerBound': lower, 'projectCount': len(projects),
                              'totalPoints': total, 'totalDevices': sum(r['total'] for r in rows),
                              'collectChannels': sum(p['collectChannels'] for p in projects),
                              'forwardChannels': sum(p['forwardChannels'] for p in projects)}


def resolve(config, rows):
    sources = []
    # Read template snapshots once so the digest describes the exact parsed bytes.
    templates = {}
    for key in ('templateDbPath', 'templateBmsCsvPath'):
        pinned = config.get('templateSnapshots', {}).get(key)
        path = Path(config[key])
        if pinned:
            if not re.fullmatch(r'[a-f0-9]{64}', pinned):
                raise ValueError('模板快照编号无效')
            path = DATA / 'templates' / pinned
        if not path.is_file():
            raise ValueError(f'模板文件不存在：{path.name}')
        blob = path.read_bytes()
        fingerprint = sha256(blob).hexdigest()
        if pinned and fingerprint != pinned:
            raise ValueError('模板快照校验失败')
        cache = DATA / 'templates' / fingerprint
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            cache.write_bytes(blob)
        sources.append({'key': key, 'name': Path(config[key]).name, 'sha256': fingerprint})
        parsed = g.parse_template(blob, Path(config[key]).name)
        for kind, value in parsed.get('templates', {}).items():
            templates[kind.upper()] = value.get('tags', [])
    parts = g.bms_segments(templates.get('BMS', []))
    templates.update({k.upper(): v for k, v in parts.items()})
    resolved, reports = {}, []
    overrides = g.alarm_rules(config)
    for row in rows:
        if not row['total']:
            continue
        kind, size = row['type'], row['points']
        source = templates.get(kind, [])
        missing = max(0, size-len(source))
        if missing and not config.get('allowFill'):
            raise ValueError(f'{kind}模板有{len(source)}点，需要{size}点，缺少{missing}点；请补充模板或启用占位补点')
        tags = [g.normalize_tag(t, i, kind) for i,t in enumerate(source[:size],1)]
        existing_names = {t['n'] for t in tags}
        existing_descriptions = {t['d'] for t in tags}
        serial = 1
        while len(tags) < size:
            name, desc = f'FillTag{serial}', f'{kind}_FillTag{serial}'
            serial += 1
            if name in existing_names or desc in existing_descriptions:
                continue
            tags.append({'n':name,'d':desc,'r':'0','p':'1000','u':'','s0':'0','s1':'','s2':''})
            existing_names.add(name)
            existing_descriptions.add(desc)
        for i, tag in enumerate(tags, 1):
            tag['p'] = g.interval_for_index(i, rules(row))
        tags = g.apply_alarm_rule(tags, kind, config)
        hits = []
        for rule in overrides:
            if rule.get('type') != kind or not rule.get('enabled'):
                continue
            hits.append({'name': rule['description'], 'devices': row['total'],
                         'generatorType': rule['generatorType'], 'interval': rule['interval']})
        for key in ('n', 'd'):
            values = [t[key] for t in tags]
            if any(not v.strip() for v in values) or len(set(values)) != size or (key == 'n' and 'device_status' in values):
                raise ValueError(f'{kind}模板标签名或测点描述为空、重复或使用保留名称')
        if kind in g.BMS_DERIVED_TYPES:
            sizes = {r['type']: r['points'] for r in rows}
            offset = 0 if kind == 'BMS' else sizes['BMS']
            if kind == 'CELL':
                offset += sizes['GROUPBMS']
            for i, tag in enumerate(tags, 1):
                tag['n'] = f'Tag{offset+i}'
        resolved[kind] = tags
        reports.append({'type': kind, 'templatePoints': len(source), 'realPoints': min(size,len(source)),
                        'fillPoints': missing, 'periods': dict(Counter(int(t['p']) for t in tags)), 'overrides': hits,
                        'sample': [{'tag': t['n'], 'description': t['d'], 'customize_info': t['d'],
                                    'interval': int(t['p']), 'generator': int(t['s0'])} for t in tags]})
    return resolved, reports, sources


def prepare(payload):
    config, rows = normalize(payload)
    planned, projects, summary = allocate(config, rows)
    resolved, reports, sources = resolve(config, rows)
    config['templateSnapshots'] = {s['key']: s['sha256'] for s in sources}
    snapshot = {'config': config, 'types': rows}
    plan = {'ok': True, 'snapshot': snapshot, 'projects': projects, 'summary': summary,
            'templates': reports, 'sources': sources, 'planId': digest({'snapshot':snapshot, 'sources':sources, 'version':VERSION})}
    return plan, planned, resolved


def iter_projects(plan, planned, resolved):
    counters = Counter()
    credential_counters = Counter()
    config = deepcopy(plan['snapshot']['config'])
    config['_bmsSegments'] = {r['type']: r['points'] for r in planned if r['type'] in g.BMS_DERIVED_TYPES}
    config['_bmsPrefixes'] = {r['type']: r['prefix'] for r in planned if r['type'] in g.BMS_DERIVED_TYPES}
    for summary in plan['projects']:
        project = g.empty_project(summary['name'])
        for row in planned:
            kind = row['type']
            if kind in ('GROUPBMS', 'CELL'):
                continue
            tags = (sum((resolved.get(k, []) for k in ('BMS', 'GROUPBMS', 'CELL')), [])
                    if kind == 'BMS' else resolved.get(kind, []))
            for _ in range(summary['counts'][kind]):
                counters[kind] += 1
                name = f"C{len(project['corridors_eq'])+1}"
                corridor = {'name': name, 'alias_name': f"{row['prefix']}{counters[kind]}", 'used': True,
                            'type': 'simulate_enhanced', 'class_name': 'SimulateEnhancedConnector',
                            'server': {'disconnected_option': 0, 'devices': [{'refresh_interval': 1.0,
                            'point_table': [g.device_status_point()] + [g.collect_point(t) for t in tags],
                            'deviceName': 'D01', 'alias_name': '设备01', 'used': True}]}}
                project['corridors_eq'].append(corridor)
                if config['forwardMode'] == 'direct':
                    for alias, points in g.collect_forward_groups(corridor, config):
                        g.add_forward_channel_for_collect(project, config, corridor, credential_counters, alias, points)
        if config['forwardMode'] == 'aggregate':
            g.add_aggregate_forward_channels(project, config, credential_counters)
        yield project, summary


def validate_project(project, expected, config, seen_ids, resolved, credential_counts=None):
    credential_counts = credential_counts if credential_counts is not None else Counter()
    collects, forwards = project['corridors_eq'], project['corridors_zf']
    if project['project_name'] != expected['name'] or len(collects) != expected['collectChannels'] or len(forwards) != expected['forwardChannels']:
        raise ValueError('工程名称或通道数量不一致')
    actual_points, counts, routes = 0, Counter(), {}
    prefix_kind = {r['prefix']: r['type'] for r in config['types']}
    rows = {r['type']: r for r in config['types']}
    if len({c['name'] for c in collects}) != len(collects) or len({f['name'] for f in forwards}) != len(forwards):
        raise ValueError('通道名称重复')
    for c in collects:
        alias = c['alias_name']
        prefix = re.sub(r'\d+$', '', alias)
        kind = prefix_kind[prefix]
        if kind in ('GROUPBMS', 'CELL'):
            raise ValueError('派生设备不能单独采集')
        points = c['server']['devices'][0]['point_table']
        if points[0] != g.device_status_point():
            raise ValueError('系统状态点缺失')
        tags = points[1:]
        kinds = ('BMS', 'GROUPBMS', 'CELL') if kind == 'BMS' else (kind,)
        source = sum((resolved[k] for k in kinds), [])
        if tags != [g.collect_point(t) for t in source]:
            raise ValueError('实际采集点与模板或周期规则不一致')
        actual_points += len(tags)
        index = int(re.search(r'\d+$', alias)[0])
        offset = 0
        for target in kinds:
            logical_alias = f"{rows[target]['prefix']}{index}"
            if config['config']['forwardMode'] == 'aggregate':
                logical_alias = g.platform_device_name(config['config'], logical_alias)
            if logical_alias in routes:
                raise ValueError('设备别名重复')
            size = rows[target]['points']
            routes[logical_alias] = (c, tags[offset:offset+size], target, index)
            offset += size
    for f in forwards:
        devices = f['server']['devices']
        if not devices or len({d['deviceName'] for d in devices}) != len(devices):
            raise ValueError('转发通道缺少设备或设备名称重复')
        grouped = config['config']['forwardMode'] == 'aggregate'
        expected_tools = ('message_platform', 'rpc_tool_platform')
        if (f['server'].get('message_tool'), f['server'].get('rpc_tool')) != expected_tools:
            raise ValueError('转发模式与报文处理器不一致')
        if not grouped and (len(devices) != 1 or devices[0]['alias_name'] != f['alias_name']):
            raise ValueError('直连通道必须只包含对应设备')
        members = []
        for device in devices:
            alias = device['deviceName'] if grouped else device['alias_name']
            if grouped and device.get('alias_name') != '':
                raise ValueError('汇总设备应直接使用设备名称，别名应为空')
            if grouped and device.get('change') is not True:
                raise ValueError('共享通道设备change必须为布尔值true')
            if alias not in routes:
                raise ValueError('转发设备重复或无对应采集设备')
            c, tags, kind, index = routes.pop(alias)
            members.append((kind,index))
            counts[kind] += 1
            outgoing = device['point_table']
            if len(outgoing) != len(tags):
                raise ValueError('转发设备或点数不一致')
            for t, out in zip(tags, outgoing):
                path = f"{c['name']}.D01.{t['tag']}"
                expected_tag = t['tag'] if grouped else path
                if out['point_key_in'] != path or out['tag'] != expected_tag or out['customize_info'] != t['name'] or out['name'] != f"{c['alias_name']} 设备01 {t['name']}" or out['valueType'] != t['valueType']:
                    raise ValueError('测点映射或描述不一致')
        kind, first = members[0]
        category = rows[kind]['prefix'].lower()
        if grouped:
            if members != [(kind, n) for n in range(first,first+len(members))]:
                raise ValueError('汇总通道内设备必须同类型且编号连续')
            last = members[-1][1]
            expected_alias = f'{category}{first}-{last}' if first != last else f'{category}{first}'
            if f['alias_name'] != expected_alias:
                raise ValueError('汇总通道名称与设备范围不一致')
            if len(devices)>1 and sum(len(d['point_table']) for d in devices)>config['config']['maxForwardTags']:
                raise ValueError('汇总通道点数超过上限')
            credential_counts[category] += 1
            index = credential_counts[category]
        credentials = g.mqtt_credentials(config['config'], category, index)
        for key, value in credentials.items():
            if f['server'][key] != value:
                raise ValueError('设备凭证映射不一致')
        identity = f['server']['client_id']
        if identity in seen_ids:
            raise ValueError('MQTT客户端ID重复')
        seen_ids.add(identity)
    if routes or actual_points != expected['points'] or any(counts[k] != v for k,v in expected['counts'].items()):
        raise ValueError('最终文件数量或点数校验失败')


def generate_run(payload, progress=lambda *args: None, run_id=None):
    plan, planned, resolved = prepare(payload)
    if payload.get('planId') != plan['planId']:
        raise ValueError('配置或模板已变化，请重新计算分配')
    run_id = run_id or uuid4().hex
    root = DATA / 'runs' / run_id
    root.mkdir(parents=True, exist_ok=False)
    partial = root / 'package.partial'
    summaries, seen_ids, credential_counts = [], set(), Counter()
    started = datetime.now().isoformat(timespec='seconds')
    try:
        with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for index, (project, expected) in enumerate(iter_projects(plan, planned, resolved), 1):
                entry = f"{expected['name']}/tb_gateway.json"
                archive.writestr(entry, json.dumps(project, ensure_ascii=False, separators=(',', ':')))
                del project
                written = json.loads(archive.read(entry))
                validate_project(written, expected, plan['snapshot'], seen_ids, resolved, credential_counts)
                del written
                summaries.append(expected)
                progress(index, len(plan['projects']))
            report = {'version': VERSION, 'planId': plan['planId'], 'sources': plan['sources'],
                      'summary': plan['summary'], 'projects': summaries, 'passed': True,
                      'platformVerified': False, 'started': started,
                      'templates': [{k:v for k,v in t.items() if k!='sample'} for t in plan['templates']]}
            archive.writestr('validation.json', json.dumps(report, ensure_ascii=False, indent=2))
        partial.replace(root / 'gateway-projects.zip')
        snapshot = deepcopy(plan['snapshot'])
        snapshot['config']['templateSnapshots'] = {s['key']: s['sha256'] for s in plan['sources']}
        (root/'snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        record = {**report, 'id': run_id, 'name': str(payload.get('name') or '未命名方案')[:100],
                  'bytes': (root/'gateway-projects.zip').stat().st_size}
        (root/'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        return record
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def save_scheme(payload):
    config, rows = normalize(payload)
    _, _, sources = resolve(config, rows)
    config['templateSnapshots'] = {s['key']: s['sha256'] for s in sources}
    folder = DATA / 'schemes'
    folder.mkdir(parents=True, exist_ok=True)
    value = {'id': uuid4().hex, 'name': str(payload.get('name') or '未命名方案')[:100],
             'created': datetime.now().isoformat(timespec='seconds'), 'version': VERSION,
             'config': config, 'types': rows}
    (folder/(value['id']+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'ok': True, 'id': value['id']}


def delete_scheme(payload):
    scheme_id = payload.get('id') if isinstance(payload, dict) else None
    if not isinstance(scheme_id, str):
        raise ValueError('方案编号无效')
    scheme_id = safe_id(scheme_id)
    folder = (DATA / 'schemes').resolve()
    source = folder / (scheme_id + '.json')
    trash = (DATA / 'trash' / 'schemes').resolve()
    destination = trash / (scheme_id + '.json')
    if not folder.is_relative_to(DATA.resolve()) or not source.resolve().is_relative_to(folder) or not trash.is_relative_to(DATA.resolve()):
        raise ValueError('方案路径无效')
    if not source.is_file():
        if destination.is_file():
            return {'ok': True, 'id': scheme_id, 'deleted': False}
        raise ValueError('方案不存在或已删除')
    if destination.exists():
        raise ValueError('回收目录中存在同编号方案，未执行删除')
    trash.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return {'ok': True, 'id': scheme_id, 'deleted': True}


def listing(kind):
    pattern = '*/record.json' if kind == 'runs' else '*.json'
    values = [json.loads(p.read_text(encoding='utf-8')) for p in (DATA/kind).glob(pattern)]
    if kind == 'runs':
        for path in (DATA/'jobs').glob('*.json'):
            job = json.loads(path.read_text(encoding='utf-8'))
            if job.get('state') == 'failed':
                values.append({'id': path.stem, 'name': job.get('name', '未命名方案'),
                               'started': job.get('started', ''), 'passed': False,
                               'error': job.get('error', '生成未完成'),
                               'summary': {'projectCount': job.get('total', 0), 'totalPoints': 0}})
    return sorted(values, key=lambda x: x.get('started', x.get('created', '')), reverse=True)


def write_job(job_id, value):
    folder = DATA / 'jobs'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (safe_id(job_id) + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def read_job(job_id):
    path = DATA / 'jobs' / (safe_id(job_id) + '.json')
    if not path.is_file():
        return {'state': 'failed', 'error': '任务不存在，请查看生成记录'}
    value = json.loads(path.read_text(encoding='utf-8'))
    if value['state'] == 'running':
        record = DATA / 'runs' / job_id / 'record.json'
        if record.is_file():
            return {**value, 'state': 'complete'}
        value.update(state='failed', error='服务停止导致生成中断，请重新生成；未校验完成的包不可下载')
    return value


def safe_id(value):
    if not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('记录编号无效')
    return value
