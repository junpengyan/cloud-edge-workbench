"""Immutable selections from a versioned, local device pool; never writes to the platform."""
import copy
from collections import Counter
import workspace as s


def pool_for(project, mode="snapshot"):
    if mode not in ("snapshot", "latest"):
        raise s.Problem("设备池版本选项无效")
    if mode == "latest":
        return s.get_project(project.get("testRound", {}).get("poolId", project["id"]))
    meta = project.get('testRound')
    if not meta:
        return project
    return s.read(s.DATA / 'projects' / s.valid_id(meta['poolId']) / ('v%d.json' % meta['poolRevision']))


def info(pid, mode="snapshot"):
    source = s.get_project(pid)
    pool = pool_for(source, mode)
    counts = Counter(b['businessType'] for b in pool['bindings'])
    return {'poolId': pool['id'], 'poolRevision': pool['revision'], 'poolName': pool['name'],
            'poolDevices': len(pool['bindings']), 'counts': dict(counts),
            'selectedCounts': {r['type']: r['total'] for r in source['types']}, 'poolMode':mode}


def selection(pid, body):
    source = s.get_project(pid)
    if source['revision'] != body.get('revision'):
        raise s.Problem('工程版本已变化，请重新打开后创建轮次', 409)
    pool = pool_for(source, body.get('poolMode', 'snapshot'))
    if body.get('poolMode') == 'latest' and body.get('poolRevision') != pool['revision']:
        raise s.Problem('设备池已更新，请重新选择版本并预览', 409)
    if pool.get('profile') != source.get('profile') or pool['cloud']['base_url'].rstrip('/') != source['cloud']['base_url'].rstrip('/'):
        raise s.Problem('设备池方案或平台已变化，请直接从最新设备池新建轮次')
    if not pool['bindings'] or pool['sourceStatus'] != 'SUCCESS' or pool['mappingIssues']:
        raise s.Problem('请先完成设备池身份绑定和组合关系确认')
    kinds = s.kinds_for(pool.get('profile', 'storage'))
    counts = body.get('counts')
    if not isinstance(counts, dict) or set(counts) != set(kinds):
        raise s.Problem('请填写每类设备的本轮数量')
    available = Counter(b['businessType'] for b in pool['bindings'])
    for kind, count in counts.items():
        if type(count) is not int or not 0 <= count <= available[kind]:
            raise s.Problem(kind + ' 数量必须在 0 至设备池可用数量之间')
    if not sum(counts.values()):
        raise s.Problem('本轮至少选择一台设备')
    if pool.get('profile', 'storage') == 'storage' and len({counts[k] for k in ('BMS','GROUPBMS','CELL')}) != 1:
        raise s.Problem('BMS、GROUPBMS、CELL 必须整组选择，三类数量须相同')
    chosen = []
    for kind in kinds:
        rows = [b for b in pool['bindings'] if b['businessType'] == kind]
        # stackIndex is the confirmed combination order; alias order is fixed in the pool snapshot.
        rows.sort(key=lambda b: (b.get('stackIndex') or int(''.join(c for c in b['collectAlias'] if c.isdigit()) or 0), b['platformId']))
        chosen.extend(copy.deepcopy(rows[:counts[kind]]))
    result = copy.deepcopy(source)
    result['bindings'] = chosen
    shared={(b.get('platformUrl'),b['platformName']):copy.deepcopy(b) for b in pool.get('sharedBindings',[])}
    if body.get('poolMode', 'snapshot') == 'snapshot':
        shared.update({(b.get('platformUrl'),b['platformName']):copy.deepcopy(b) for b in source.get('sharedBindings',[])})
    result['sharedBindings']=list(shared.values())
    ids = {b['platformId'] for b in chosen}
    result['relations'] = [r for r in pool['relations'] if r['from'] in ids and r['to'] in ids]
    result['mappingIssues'] = []
    for row in result['types']:
        row['total'] = counts[row['type']]
    for kind in kinds:
        for i, binding in enumerate((b for b in chosen if b['businessType'] == kind), 1):
            binding['collectAlias'] = kind.lower() + str(i)
            if binding.get('stackIndex') is not None:
                binding['stackIndex'] = i
    result['testRound'] = {'poolId': pool['id'], 'poolRevision': pool['revision'],
        'poolName': pool['name'], 'poolDevices': len(pool['bindings']), 'sourceProjectId': source['id'],
        'sourceRevision': source['revision'], 'selectionMode': 'fixed_order', 'poolMode':body.get('poolMode','snapshot'),
        'selectedIds': [b['platformId'] for b in chosen], 'counts': counts,
        'previousCounts': {r['type']:r['total'] for r in source['types']}}
    return result


def preview(pid, body):
    result = selection(pid, body)
    old = {b['platformId']:b for b in s.get_project(pid)['bindings']}
    new = {b['platformId']:b for b in result['bindings']}
    changes = {'added':len(new.keys()-old.keys()), 'removed':len(old.keys()-new.keys()),
               'credentialsChanged':sum(new[k]['credentialRef'] != old[k]['credentialRef'] for k in new.keys() & old.keys())}
    return {**result['testRound'], 'changes':changes, 'devices': len(result['bindings']),
            'points': sum(r['total'] * (r['points'] or 0) for r in result['types']),
            'sample': [{'type': b['businessType'], 'name': b['platformName'], 'id': b['platformId']} for b in result['bindings'][:20]],
            'selection': [{'type':b['businessType'],'name':b['platformName'],'id':b['platformId']} for b in result['bindings']]}


def create(pid, body):
    with s.LOCK:
        if any(j.get('projectId')==pid and j['state']=='running' for j in s.jobs()): raise s.Problem('工程任务运行中，请稍后创建轮次',409)
        result = selection(pid, body)
        result.update(id=s.uid(), revision=1, name=(str(body.get('name') or '新测试轮次').strip()[:100] or '新测试轮次'),
                      created=s.now(), updated=s.now())
        result.pop('deletedAt', None)
        s.save_project(result)
        s.audit('create_round', result['id'], 'completed')
        return result


def refresh_pool(pid, body):
    """Fetch credentials by stable platform ID, then commit one new pool revision."""
    from app.platform.thingsboard_client import ThingsBoardClient
    from app.core.orchestrator import _extract_mqtt_credentials
    source = s.get_project(pid)
    if source.get('testRound'):
        raise s.Problem('请打开原设备池工程刷新；已有轮次保持原快照')
    if source['revision'] != body.get('revision') or not source['bindings']:
        raise s.Problem('请重新打开已绑定的设备池工程',409)
    credentials = body.get('auth', {})
    username = credentials.get('existing_tenant_admin_username')
    password = credentials.get('existing_tenant_admin_password')
    if not isinstance(username,str) or not username.strip() or not isinstance(password,str) or not password:
        raise s.Problem('请填写租户管理员账号和密码')
    client = ThingsBoardClient(source['cloud']['base_url'])
    fetched = []
    shared_fetched=[]
    try:
        client.login(username,password)
        for binding in source['bindings']:
            raw = client.get_device_credentials(binding['platformId'])
            client_id,user,secret,_ = _extract_mqtt_credentials(raw)
            fetched.append(s.credentials_of({'id':binding['platformId'], 'mqtt_client_id':client_id,
                'mqtt_username':user,'mqtt_password_or_token':secret,'raw':{'credentials':raw}},{}))
        for binding in source.get('sharedBindings',[]):
            if binding.get('platformUrl')!=source['cloud']['base_url'].rstrip('/') or binding.get('credentialsPending'): continue
            raw=client.get_device_credentials(binding['platformId'])
            client_id,user,secret,_ = _extract_mqtt_credentials(raw)
            shared_fetched.append((binding['platformId'],s.credentials_of({'id':binding['platformId'], 'mqtt_client_id':client_id,
                'mqtt_username':user,'mqtt_password_or_token':secret,'raw':{'credentials':raw}},{})))
    except Exception:
        raise s.Problem('设备池刷新失败：请检查平台地址、账号及设备访问权限；原绑定未更改')
    finally:
        client.close()
    if len({c['client_id'] for c in fetched}) != len(fetched):
        raise s.Problem('设备池存在重复 MQTT 客户端 ID；原绑定未更改')
    with s.LOCK:
        current = s.get_project(pid)
        if current['revision'] != source['revision']:
            raise s.Problem('设备池已变化，请重新刷新',409)
        if any(j.get('projectId')==pid and j['state']=='running' for j in s.jobs()):
            raise s.Problem('工程任务运行中，请完成后刷新',409)
        for binding, value in zip(current['bindings'],fetched):
            binding['credentialRef'] = s.put_secret(value)
        for device_id,value in shared_fetched:
            binding=next(b for b in current.get('sharedBindings',[]) if b['platformId']==device_id and b.get('platformUrl')==source['cloud']['base_url'].rstrip('/'))
            binding['credentialRef']=s.put_secret(value)
        current['revision'] += 1
        current['updated'] = s.now()
        s.save_project(current)
        return current


def overview():
    """Small project summaries grouped by pool; never return credentials or full bindings."""
    import deployment
    projects = s.projects()
    generation, deployments = {}, {}
    for job in s.jobs():
        if job.get('kind') == 'gateway':
            generation.setdefault(job['projectId'], job)
    for record in deployment.records():
        deployments.setdefault(record['projectId'], record)
    for project in projects:
        job = generation.get(project['id'])
        record = deployments.get(project['id'])
        project['generation'] = None if not job else {k:job.get(k) for k in ('id','state','created','filesValid')}
        if job:
            try:
                plan = s.read(s.DATA/'plans'/(s.valid_id(job['planId'])+'.json'))
                project['generation']['revision'] = plan['revision']
                project['generation']['current'] = plan['revision'] == project['revision']
            except (s.Problem, KeyError):
                project['generation']['current'] = False
        project['deployment'] = None if not record else {k:record.get(k) for k in ('id','state','revision','configuredDevices','observedDevices','assessmentAt')}
        if record:
            project['deployment']['acceptanceState'] = record.get('acceptanceResult',{}).get('state')
            project['deployment']['reportedDevices'] = record.get('telemetryCheck',{}).get('reported')
    return projects


def allocate(pid, body):
    """Capped proportional allocation in complete device groups; never exceeds target."""
    source = s.get_project(pid)
    if source['revision'] != body.get('revision'):
        raise s.Problem('工程版本已变化，请重新打开', 409)
    pool = pool_for(source, body.get('poolMode', 'snapshot'))
    if body.get('poolMode') == 'latest' and body.get('poolRevision') != pool['revision']:
        raise s.Problem('设备池已更新，请重新选择版本', 409)
    available = Counter(b['businessType'] for b in pool['bindings'])
    target = body.get('targetDevices')
    if type(target) is not int or not 1 <= target <= sum(available.values()):
        raise s.Problem('目标总数须在 1 至设备池总数之间')
    kinds = s.kinds_for(pool.get('profile', 'storage'))
    groups = [('BMS','GROUPBMS','CELL')] if pool.get('profile') != 'microgrid' else []
    groups += [(k,) for k in kinds if not any(k in g for g in groups)]
    caps = [min(available[k] for k in g) for g in groups]
    previous = {r['type']:r['total'] for r in source['types']}
    weights = [sum(previous[k] for k in g) for g in groups]
    if not sum(weights): weights = [c*len(g) for c,g in zip(caps,groups)]
    ideal = [target*w/sum(weights) for w in weights]
    units = [0]*len(groups); total = 0
    def reachable(remaining, used):
        singles = sum(c-used[i] for i,(c,g) in enumerate(zip(caps,groups)) if len(g)==1)
        triples = sum(c-used[i] for i,(c,g) in enumerate(zip(caps,groups)) if len(g)==3)
        return max(0,(remaining-singles+2)//3) <= min(triples,remaining//3)
    actual_target = target
    while actual_target and not reachable(actual_target, units): actual_target -= 1
    while True:
        eligible = []
        for i,g in enumerate(groups):
            if units[i] >= caps[i] or total+len(g) > actual_target: continue
            trial = units.copy(); trial[i] += 1
            if reachable(actual_target-total-len(g),trial): eligible.append(i)
        if not eligible: break
        i = max(eligible, key=lambda i:(ideal[i]-units[i]*len(groups[i]))/len(groups[i]))
        units[i] += 1; total += len(groups[i])
    counts = {k:units[i] for i,g in enumerate(groups) for k in g}
    return {'targetDevices':target, 'actualDevices':total, 'difference':total-target, 'counts':counts,
            'note':'按来源比例分配并遵守库存及整组约束；请核对实际数量后预览确认。'}
