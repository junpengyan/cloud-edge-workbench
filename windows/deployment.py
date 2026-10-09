"""Temporary gateway deployment with automatic service recovery; telemetry is checked manually."""
import copy
import acceptance
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import threading
import time
import zipfile
from collections import Counter
import workspace as s

if s.os.name == 'nt':
    sys.path.append(str(s.BASE / '.validation-deps'))
import paramiko

ROOT = s.DATA / 'deployments'
ROOT.mkdir(exist_ok=True)
LOCK = threading.RLock()
ACTIVE = set()

def folder(jid):
    return ROOT / s.valid_id(jid)

def record(jid):
    j = s.read(folder(jid) / 'record.json')
    if j.get('telemetryCheck',{}).get('state') == 'running' and jid not in acceptance.ACTIVE:
        j['telemetryCheck'].update(state='failed',message='核验进程已中断，请重新执行只读核验')
    return j

def save(j):
    with LOCK:
        path = folder(j['id']) / 'record.json'
        if path.exists():
            previous = s.read(path)
            if previous.get('assessmentAt','') > j.get('assessmentAt',''):
                for key in ('assessmentAt','assessmentNotes','observedDevices'):
                    j[key] = previous[key]
            if previous.get('resultUpdatedAt',0) > j.get('resultUpdatedAt',0):
                for key in ('resultUpdatedAt','telemetryCheck','telemetryHistory','acceptanceResult','measuredMetrics','measurementWindow','assessmentHistory','assessmentAt','assessmentNotes','observedDevices'):
                    if key in previous: j[key] = previous[key]
        s.atomic(path, j)

def records():
    return sorted([record(p.parent.name) for p in ROOT.glob('*/record.json')], key=lambda j:j['created'], reverse=True)

def options(body, name_limit=40):
    host = str(body.get('host', '')).strip()
    name = str(body.get('projectName', 'test4'))
    deployment_mode = body.get('deploymentMode', 'validation')
    if deployment_mode not in ('validation', 'formal'):
        raise s.Problem('部署方式无效')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host):
        raise s.Problem('请填写有效的网关主机地址')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,' + str(name_limit-1) + r'}', name):
        raise s.Problem('远程工程名称须以字母开头，只能包含字母、数字、下划线和短横线')
    port = body.get('port', 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise s.Problem('SSH 端口无效')
    duration = body.get('durationSeconds', 300)
    if type(duration) is not int or not 10 <= duration <= 604800:
        raise s.Problem('运行时长须为 10 至 604800 秒')
    warmup = body.get('warmupSeconds',0)
    if type(warmup) is not int or not 0 <= warmup < duration:
        raise s.Problem('预热时间须大于等于 0 且小于总运行时长')
    if duration-warmup < 10: raise s.Problem('正式采样时间至少为 10 秒')
    targets = acceptance.criteria(body.get('criteria') or {})
    replace = body.get('replaceProjects', [])
    if not isinstance(replace, list) or len(replace) > 1000 or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', n) for n in replace) or len(set(replace)) != len(replace):
        raise s.Problem('上一轮工程目录名称无效或重复')
    previous_id = body.get('previousDeploymentId') or None
    if previous_id:
        previous = record(previous_id)
        if previous['options']['host'] != host or previous['options']['port'] != port:
            raise s.Problem('上一轮记录与当前目标网关不一致')
        if previous['state'] in ('starting','running','recovering','recovery_required'):
            raise s.Problem('上一轮尚未恢复，请先停止并确认恢复完成')
        if replace:
            raise s.Problem('请选择历史记录或手填目录，不能同时使用')
        replace = previous['options'].get('projectNames',[previous['options']['projectName']])
    return {'host':host, 'port':port, 'projectName':name, 'deploymentMode':deployment_mode,
            'durationSeconds':duration, 'replaceProjects':replace,
            'previousDeploymentId':previous_id, 'warmupSeconds':warmup, 'sampleSeconds':duration-warmup, 'criteria':targets,
            'remotePath':'/root/gateway/thingsboard_gateway/config/' + name}

def prepare(pid, body):
    p = s.get_project(pid)
    if p['revision'] != body.get('revision'):
        raise s.Problem('工程已变化，请刷新后重新准备', 409)
    if p.get('profile') != 'microgrid':
        raise s.Problem('当前在线验证支持微电网四类设备；储能工程继续使用原生成流程')
    opt = options(body)
    subset = copy.deepcopy(p)
    selected = []
    for row in subset['types']:
        matches = [b for b in p['bindings'] if b['businessType'] == row['type']]
        if not matches:
            raise s.Problem(row['type'] + ' 尚未绑定平台设备')
        b = copy.deepcopy(matches[0])
        b['collectAlias'] = row['type'].lower() + '1'
        selected.append(b)
        row['total'] = 1
    subset['bindings'] = selected
    subset['gateway'].update(projectPrefix=opt['projectName'], maxProjectPoints=20000000)
    plan, planned, resolved = s.engine(subset).prepare(s.payload_for(subset))
    generated = list(s.engine(subset).iter_projects(plan, planned, resolved))
    if len(generated) != 1:
        raise s.Problem('本阶段验证包必须为一个工程')
    config, expected = generated[0]
    s.engine(subset).validate_project(config, expected, plan['snapshot'], set(), resolved, Counter())
    s.verify_bindings(config, subset, set())
    config['project_name'] = opt['projectName']
    devices = [{'type':b['businessType'], 'name':b['platformName'], 'id':b['platformId'],
                'points':next(r['points'] for r in subset['types'] if r['type']==b['businessType']),
                'fastPoints':next(r['fastPoints'] for r in subset['types'] if r['type']==b['businessType'])} for b in selected]
    jid = s.uid()
    j = {'id':jid, 'projectId':pid, 'revision':p['revision'], 'sourceName':p['name'],
         'fingerprint':s.digest(p), 'created':s.now(), 'state':'prepared', 'options':opt,
         'platformUrl':p['cloud']['base_url'].rstrip('/'), 'mqttHost':p['gateway']['mqttHost'],
         'mqttPort':p['gateway']['mqttPort'], 'devices':devices, 'filesValid':True,
         'platformVerified':False, 'verificationMode':'manual', 'message':'四台设备验证包已生成，尚未部署'}
    for device in devices:
        device['expectedKeys'] = [key for _,member,binding in s.forward_members(config,subset) if binding['platformId']==device['id'] for key in acceptance.telemetry_keys_of(member)]
    acceptance.freeze(j, {opt['projectName']:config})
    folder(jid).mkdir()
    (folder(jid)/'config.dpapi').write_bytes(s.crypt(json.dumps(config).encode()))
    save(j)
    return j


def generated_sources():
    """List validated generation batches, including storage, newest first."""
    result = []
    for job in sorted(s.jobs(), key=lambda x:x.get('created',''), reverse=True):
        if job.get('kind') != 'gateway' or job.get('state') != 'completed' or not job.get('filesValid'):
            continue
        try:
            s.get_project(job['projectId'])
            report = s.read(s.DATA/'packages'/s.valid_id(job['id'])/'validation.json')
            if not (s.DATA/'packages'/job['id']/'gateway-projects.zip').is_file():
                continue
            result.append({'id':job['id'], 'projectId':job['projectId'], 'name':job['projectName'],
                           'created':job['created'], 'revision':report['revision'], 'projects':report['projects'], 'testRound':bool(report.get('testRound'))})
        except (s.Problem, KeyError):
            continue
    return result

def prepare_generated(jid, body):
    job = s.read(s.job_path(jid))
    if job.get('kind') != 'gateway' or job.get('state') != 'completed' or not job.get('filesValid'):
        raise s.Problem('请选择已生成且校验通过的网关工程',409)
    s.get_project(job['projectId'])  # Deleted sources must be restored before deployment.
    plan = s.read(s.DATA/'plans'/(s.valid_id(job['planId'])+'.json'))
    p = s.read(s.DATA/'projects'/s.valid_id(job['projectId'])/('v%d.json'%plan['revision']))
    if s.digest(p) != plan['fingerprint']:
        raise s.Problem('生成版本快照不一致，请重新生成工程',409)
    member = body.get('member')
    expected_rows = plan['public']['projects']
    allowed = {x['name'] for x in expected_rows}
    if not isinstance(member, str) or (member != '*' and member not in allowed):
        raise s.Problem('请选择整个生成批次或具体网关工程')
    selected = expected_rows if member == '*' else [x for x in expected_rows if x['name'] == member]
    if p.get('testRound') and member != '*':
        raise s.Problem('测试轮次必须部署整个生成批次，不能只部署一个子工程')
    preserve_names = str(body.get('projectName') or '').strip() == ''
    opt = options({**body, 'projectName':selected[0]['name'] if preserve_names else body['projectName']},
                  name_limit=80 if preserve_names else 40)
    if preserve_names and any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', row['name']) for row in selected):
        raise s.Problem('生成工程名称不适合作为远程目录，请手动指定远程工程名称')
    configs, devices, seen = {}, [], set()
    credential_sets=s.shared_credentials(p) if p['gateway']['forwardMode']=='aggregate' else None
    bindings = {b['collectAlias']: b for b in p['bindings']}
    path = s.DATA/'packages'/s.valid_id(jid)/'gateway-projects.zip'
    try:
        with zipfile.ZipFile(path) as archive:
            for i, expected in enumerate(selected, 1):
                config = json.loads(archive.read(expected['name']+'/tb_gateway.json'))
                s.verify_bindings(config, p, seen,credential_sets)
                current = []
                for channel,device,binding in s.forward_members(config,p):
                    keys = acceptance.telemetry_keys_of(device)
                    if not keys or len(set(keys)) != len(keys):
                        raise s.Problem('生成工程的设备测点缺失或重复')
                    current.append({'type':binding['businessType'], 'name':binding['platformName'],
                                    'id':binding['platformId'], 'points':len(keys), 'expectedKeys':keys})
                if len(config['corridors_zf']) != expected['forwardChannels'] or len(current) != expected['devices'] or sum(x['points'] for x in current) != expected['points']:
                    raise s.Problem('工程内容与生成报告不一致，请重新生成',409)
                name = expected['name'] if preserve_names else opt['projectName'] if len(selected) == 1 else opt['projectName']+'_'+str(i)
                config['project_name'] = name
                configs[name] = config
                devices.extend(current)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, s.Problem):
            raise
        raise s.Problem('生成工程文件缺失或损坏，请重新生成',409)
    if member == '*' and seen != {b['collectAlias'] for b in s.active_bindings(p)}:
        raise s.Problem('整轮工程未完整覆盖本轮设备',409)
    opt['projectNames'] = list(configs)
    opt['remotePaths'] = ['/root/gateway/thingsboard_gateway/config/' + name for name in configs]
    opt['remotePath'] = '、'.join(opt['remotePaths'])
    depid = s.uid()
    record_value = {'id':depid, 'projectId':p['id'], 'revision':p['revision'], 'sourceName':job['projectName'],
        'sourceMode':'generated', 'sourceJobId':jid, 'sourceMember':member,
        'testRound':p.get('testRound'), 'poolDevices':p.get('testRound',{}).get('poolDevices',len(p['bindings'])),
        'configuredDevices':len(devices), 'configuredPoints':sum(x['points'] for x in devices),
        'forwardChannels':sum(len(x['corridors_zf']) for x in configs.values()), 'observedDevices':None,
        'fingerprint':s.digest(p), 'created':s.now(), 'state':'prepared', 'options':opt,
        'platformUrl':p['cloud']['base_url'].rstrip('/'), 'mqttHost':p['gateway']['mqttHost'],
        'mqttPort':p['gateway']['mqttPort'], 'devices':devices, 'filesValid':True,
        'platformVerified':False, 'verificationMode':'manual',
        'message':str(len(devices))+'台设备 / '+str(len(configs))+'个子工程已准备；尚未部署'}
    record_value['templateMode']=p.get('templateMode',False)
    if record_value['templateMode']:
        record_value['message']+='；含模板凭据，请先导入平台 CSV 并核对，平台设备 ID 缺失时无法自动核验遥测'
    acceptance.freeze(record_value, configs)
    folder(depid).mkdir()
    stored = next(iter(configs.values())) if len(configs) == 1 else {'projects':configs}
    (folder(depid)/'config.dpapi').write_bytes(s.crypt(json.dumps(stored).encode()))
    save(record_value)
    return record_value


def configs_for(jid):
    value = config_for(jid)
    return value['projects'] if 'projects' in value else {value.get('project_name', record(jid)['options']['projectName']):value}

def config_for(jid):
    return json.loads(s.crypt((folder(jid)/'config.dpapi').read_bytes(), True))

def package(jid):
    j = record(jid)
    from task_summary import deployment_archive_summary
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        configs = configs_for(jid)
        for name, config in configs.items():
            z.writestr(name+'/tb_gateway.json', json.dumps(config, ensure_ascii=False))
        z.writestr('summary.md', deployment_archive_summary(j.get('sourceName', j['options']['projectName']), configs, j))
        z.writestr('validation.json', json.dumps(j, ensure_ascii=False, indent=2))
    return out.getvalue()

def auth(body):
    result = {k:str(body.get(k,'')) for k in ('sshUsername','sshPassword')}
    required = result.values()
    if not all(required) or any(len(v)>1024 for v in result.values()):
        raise s.Problem('请填写 SSH 登录信息')
    return result

def connect(j, credentials):
    c = paramiko.SSHClient()
    c.load_system_host_keys()
    # New hosts are pinned locally on first successful preflight; changed keys are rejected.
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    o = j['options']
    try:
        c.connect(o['host'], port=o['port'], username=credentials['sshUsername'], password=credentials['sshPassword'],
                  look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=10, banner_timeout=10)
        key = hashlib.sha256(c.get_transport().get_remote_server_key().asbytes()).hexdigest()
        path = ROOT / 'host-keys.json'
        with LOCK:
            pins = s.read(path) if path.exists() else {}
            host = o['host']+':'+str(o['port'])
            if host in pins and pins[host] != key:
                raise s.Problem('网关 SSH 主机密钥已变化，请核实服务器身份')
            if host not in pins:
                pins[host] = key
                s.atomic(path, pins)
        return c
    except Exception:
        c.close()
        raise

def remote(c, command, timeout=30):
    _, out, err = c.exec_command(command, timeout=timeout)
    value = out.read().decode('utf-8', 'replace')
    if out.channel.recv_exit_status():
        raise s.Problem('网关检查失败，请检查目录权限或系统服务')
    return value

def preflight_error_message(errors):
    messages = {
        'service_inactive': '网关服务 sprixin-gateway 未处于运行状态。请在目标网关执行 systemctl status sprixin-gateway --no-pager -l 查看状态；确认可以运行现有工程后，执行 systemctl start sprixin-gateway，再重新预检。启动服务会加载现有工程。',
        'license_missing': '未找到网关授权文件，请确认网关已完成授权后重新预检。',
        'previous_run_active': '检测到远端任务锁，上一轮可能仍在运行或恢复。请查看部署记录：运行中可提前停止，恢复中可恢复状态监测；确认恢复完成后重新预检。',
        'unsafe_path': '网关配置目录指向非预期路径，请检查 /root/gateway/thingsboard_gateway/config 的实际路径。',
    }
    named = {
        'replacement_missing_or_unsafe': '待停用工程目录缺失、为符号链接或缺少配置文件，请核对旧目录后重新准备部署包',
        'target_exists': '目标工程目录已存在，请更换远程工程名称，或将需要替换的旧工程明确列入停用范围后重新准备部署包',
        'client_conflict': '保留工程与本轮 MQTT 客户端 ID 冲突，请核对并将冲突的旧工程列入停用范围后重新准备部署包',
        'unsafe_config': '工程配置或目录为符号链接，请核实配置路径后重新预检',
    }
    details = []
    for error in errors:
        code, _, name = error.partition(':')
        details.append(messages.get(code) or (named[code]+'（'+name+'）。' if code in named else '未识别的预检错误：'+error+'。请保留错误信息并检查网关环境。'))
    return '预检未通过：'+' '.join(details)


def preflight(j, credentials):
    configs = configs_for(j['id'])
    names = [channel['server']['client_id'] for cfg in configs.values() for channel in cfg['corridors_zf']]
    if len(names) != len(set(names)):
        raise s.Problem('本轮 MQTT 客户端 ID 重复')
    with connect(j, credentials) as c:
        payload = {'targets':list(configs), 'replace':j['options'].get('replaceProjects',[]), 'clients':names, 'allowMissing':bool(j['options'].get('previousDeploymentId'))}
        code = "import pathlib,json,subprocess,hashlib\np=pathlib.Path('/root/gateway/thingsboard_gateway/config')\na=json.loads("+repr(json.dumps(payload))+ ")\n"
        code += """errors=[]
if str(p.resolve())!=str(p): errors.append('unsafe_path')
if subprocess.run(['systemctl','is-active','sprixin-gateway'],capture_output=True,text=True).stdout.strip()!='active': errors.append('service_inactive')
if not pathlib.Path('/root/.sp_gateway/.sprixin/.license_file.info').is_file(): errors.append('license_missing')
if pathlib.Path('/root/gateway/.workbench-validation-lock').exists(): errors.append('previous_run_active')
hashes={}
inventory=[]
for name in a['replace']:
 d=p/name
 if not d.exists() and not d.is_symlink() and a['allowMissing']: continue
 if d.is_symlink() or not (d/'tb_gateway.json').is_file(): errors.append('replacement_missing_or_unsafe:'+name)
 else: hashes[name]=hashlib.sha256((d/'tb_gateway.json').read_bytes()).hexdigest()
for name in a['targets']:
 if (p/name).exists() and name not in a['replace']: errors.append('target_exists:'+name)
ids=set(a['clients'])
for f in sorted(p.glob('*/tb_gateway.json')):
 if f.is_symlink() or f.parent.is_symlink(): errors.append('unsafe_config:'+f.parent.name)
 cfg=json.loads(f.read_text())
 devices=[d.get('deviceName') for c in cfg.get('corridors_zf',[]) for d in c.get('server',{}).get('devices',[])]
 inventory.append({'name':f.parent.name,'hash':hashlib.sha256(f.read_bytes()).hexdigest(),'devices':len(devices),'deviceNames':devices,'known':bool(cfg.get('corridors_zf')) and all(devices)})
 if f.parent.name not in a['replace']:
  if any(c.get('server',{}).get('client_id') in ids for c in cfg.get('corridors_zf',[])): errors.append('client_conflict:'+f.parent.name)
print(json.dumps({'errors':errors,'replacementHashes':hashes,'inventory':inventory}))
"""
        result = json.loads(remote(c, "python3 - <<'PY'\n"+code+"\nPY"))
        if result.get('errors'):
            raise s.Problem(preflight_error_message(result['errors']))
    inventory = result.get('inventory',[])
    retained = [r for r in inventory if r['name'] not in payload['replace']]
    target_names = {device['name'] for device in j.get('devices',[])}
    overlaps = sorted({name for row in retained for name in row['deviceNames'] if name in target_names})
    if overlaps: raise s.Problem('保留工程仍包含本轮设备，请明确指定停用目录后重新准备：'+', '.join(overlaps[:10]))
    switch = {'existing':inventory, 'stop':[r['name'] for r in inventory if r['name'] in payload['replace']],
              'retain':[r['name'] for r in retained], 'start':list(configs), 'selectedDevices':len(j.get('devices',[])),
              'retainedDevices':sum(r['devices'] for r in retained), 'totalKnown':all(r['known'] for r in retained)}
    switch['resultingDeviceEntries'] = switch['selectedDevices']+switch['retainedDevices']
    return {'checkedAt':s.now(), 'service':'active', 'licensePresent':True,
            'replacementHashes':result.get('replacementHashes',{}), 'switchPlan':switch, 'switchPlanId':s.digest(switch)}

def reconcile_stale_validation(j, credentials, client=None):
    """Close a validation record only after the remote run is gone and the gateway is healthy."""
    if j.get('options',{}).get('deploymentMode', 'validation') != 'validation':
        return False
    if j.get('state') not in ('starting','running','recovering','recovery_required'):
        return False
    expected = '/root/gateway/workbench-validation-' + j['id']
    if j.get('remoteRun') != expected:
        return False
    code = "import json,pathlib,subprocess\n" \
           "p=pathlib.Path('/root/gateway')\n" \
           "print(json.dumps({'run':(p/" + repr('workbench-validation-' + j['id']) + ").exists()," \
           "'lock':(p/'.workbench-validation-lock').exists()," \
           "'service':subprocess.run(['systemctl','is-active','sprixin-gateway'],capture_output=True,text=True).stdout.strip()}))"
    if client is None:
        with connect(j, credentials) as connected:
            state = json.loads(remote(connected, "python3 - <<'PY'\n" + code + "\nPY"))
    else:
        state = json.loads(remote(client, "python3 - <<'PY'\n" + code + "\nPY"))
    if state.get('run') or state.get('lock') or state.get('service') != 'active':
        return False
    j.update(state='failed', message='远端验证任务目录已不存在，网关服务运行正常；旧验证记录已结束',
             remoteStatus={'status':'missing','serviceActive':'active'}, finished=s.now())
    save(j)
    (folder(j['id'])/'auth.dpapi').unlink(missing_ok=True)
    return True

def check(jid, body):
    with LOCK:
        j = record(jid)
        if j['state'] != 'prepared':
            raise s.Problem('该记录已执行，请新建验证包后预检',409)
        result = preflight(j, auth(body))
        j['preflight'] = result
        j['verifiedReplaceProjects'] = list(result.get('replacementHashes',{}))
        save(j)
        return j

def start(jid, body):
    credentials = auth(body)
    with LOCK:
        j = record(jid)
        if j['state'] != 'prepared':
            return j
        if j.get('sourceMode') != 'generated' and s.digest(s.get_project(j['projectId'])) != j['fingerprint']:
            raise s.Problem('原工程已变更，请重新准备验证包',409)
        s.get_project(j['projectId'])
        if j.get('manifestId'):
            if s.digest(j['executionManifest']) != j['manifestId'] or j['options'] != j['executionManifest']['options']:
                raise s.Problem('执行清单不一致，请重新准备部署包',409)
            hashes = {name:hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest() for name,cfg in configs_for(jid).items()}
            if hashes != j['executionManifest']['configHashes']:
                raise s.Problem('部署文件已变化，请重新准备部署包',409)
        fresh = preflight(j, credentials)
        blockers = [x for x in records() if x['id'] != jid and x['state'] in ('starting','running','recovering','recovery_required') and x['options']['host']==j['options']['host'] and x['options']['port']==j['options']['port']]
        for old in blockers:
            if old['id'] not in ACTIVE:
                reconcile_stale_validation(old, credentials)
        if any(record(x['id'])['state'] in ('starting','running','recovering','recovery_required') for x in blockers):
            raise s.Problem('已有在线验证任务，请先等待完成或恢复',409)
        if fresh.get('switchPlanId'):
            if not j.get('preflight') or body.get('switchPlanId') != j['preflight'].get('switchPlanId') or fresh['switchPlanId'] != body.get('switchPlanId'):
                raise s.Problem('切换范围未确认或已变化，请重新预检并核对',409)
            if fresh['switchPlan']['retain'] and body.get('acknowledgeRetained') is not True:
                raise s.Problem('请确认保留工程及网关最终设备规模')
            j['options']['inventoryHashes'] = {r['name']:r['hash'] for r in fresh['switchPlan']['existing']}
            j['confirmedSwitchPlan'] = fresh['switchPlan']
        if j['options'].get('replaceProjects') and (not j.get('preflight') or j['preflight'].get('replacementHashes') != fresh['replacementHashes']):
            raise s.Problem('上一轮配置已变化，请重新预检并确认切换',409)
        j['preflight'] = fresh
        j['options']['replacementHashes'] = fresh['replacementHashes']
        j['options']['replaceProjects'] = list(fresh['replacementHashes'])
        j.update(state='starting', message='正在正式部署，完成后工程保留在网关配置目录' if j['options'].get('deploymentMode') == 'formal' else '正在部署；到达本轮运行时长后自动归档并恢复服务')
        (folder(jid)/'auth.dpapi').write_bytes(s.crypt(json.dumps(credentials).encode()))
        save(j)
        ACTIVE.add(jid)
        if j['options'].get('deploymentMode') == 'formal':
            threading.Thread(target=worker_formal,args=(jid,),daemon=True).start()
        else:
            threading.Thread(target=worker,args=(jid,True),daemon=True).start()
        return j

def worker_formal(jid):
    j = record(jid)
    try:
        credentials = json.loads(s.crypt((folder(jid)/'auth.dpapi').read_bytes(),True))
        configs = configs_for(jid)
        stage = '/root/gateway/thingsboard_gateway/config/.workbench-upload-' + jid
        j['remoteStage'] = stage
        settings = {'jobId':jid, 'projectNames':list(configs),
                    'replaceProjects':j['options'].get('replaceProjects',[]),
                    'replacementHashes':j['options'].get('replacementHashes',{}),
                    'inventoryHashes':j['options'].get('inventoryHashes',{}),
                    'stageHashes':{}}
        j['deploymentStage'] = 'staging'
        save(j)
        with connect(j,credentials) as c:
            with c.open_sftp() as f:
                created = False
                uploaded = []
                try:
                    f.mkdir(stage,0o700)
                    created = True
                    for name, config in configs.items():
                        f.mkdir(stage+'/'+name,0o700)
                        uploaded.append(name)
                        content = json.dumps(config,ensure_ascii=False)
                        settings['stageHashes'][name] = hashlib.sha256(content.encode('utf-8')).hexdigest()
                        with f.open(stage+'/'+name+'/tb_gateway.json','w') as out:
                            out.write(content)
                        f.chmod(stage+'/'+name+'/tb_gateway.json',0o600)
                except Exception:
                    if created:
                        try:
                            for name in reversed(uploaded):
                                try:
                                    f.remove(stage+'/'+name+'/tb_gateway.json')
                                except FileNotFoundError:
                                    pass
                                f.rmdir(stage+'/'+name)
                            f.rmdir(stage)
                        except Exception:
                            j['deploymentStage'] = 'cleanup_required'
                    raise
            j['deploymentStage'] = 'launching'
            save(j)
            script = (s.BASE/'formal_deploy.py').read_text(encoding='utf-8')
            command = "python3 - <<'PY'\nSETTINGS = " + repr(settings) + "\n" + script + "\nPY"
            state = json.loads(remote(c,command,timeout=180))
        j['remoteStatus'] = state
        j['options']['projectNames'] = list(configs)
        j['state'] = 'completed' if state['status'] == 'completed' else 'failed' if state['status'] == 'failed' else 'recovery_required'
        j['message'] = '正式部署完成，网关工程保留在 config 目录；请核验遥测' if j['state'] == 'completed' else '正式部署未完成，请检查远端状态与恢复信息'
        j['rollbackRestored'] = state.get('rollbackRestored',False)
        j['platformVerified'] = False
        j['verificationMode'] = 'manual'
        j['finished'] = s.now()
        save(j)
        if j['state'] in ('completed','failed'):
            (folder(jid)/'auth.dpapi').unlink(missing_ok=True)
    except Exception:
        j.update(state='failed' if j.get('deploymentStage') == 'staging' else 'recovery_required',
                 message='上传准备失败，尚未切换网关工程' if j.get('deploymentStage') == 'staging' else '正式部署状态不明，请检查网关配置目录与服务状态')
        save(j)
        if j['state'] == 'failed':
            (folder(jid)/'auth.dpapi').unlink(missing_ok=True)
    finally:
        ACTIVE.discard(jid)


def worker(jid, deploy=False):
    j = record(jid)
    try:
        credentials = json.loads(s.crypt((folder(jid)/'auth.dpapi').read_bytes(),True))
        if deploy:
            j['deploymentStage'] = 'staging'
            save(j)
            with connect(j,credentials) as c:
                r = '/root/gateway/workbench-validation-'+jid
                j['remoteRun'] = r
                save(j)
                with c.open_sftp() as f:
                    f.mkdir(r,0o700)
                    f.mkdir(r+'/staged',0o700)
                    configs = configs_for(jid)
                    j['options']['projectNames'] = list(configs)
                    for name, config in configs.items():
                        f.mkdir(r+'/staged/'+name,0o700)
                        with f.open(r+'/staged/'+name+'/tb_gateway.json','w') as out:
                            out.write(json.dumps(config,ensure_ascii=False))
                        f.chmod(r+'/staged/'+name+'/tb_gateway.json',0o600)
                    with f.open(r+'/settings.json','w') as out:
                        out.write(json.dumps(j['options'],ensure_ascii=False))
                    f.chmod(r+'/settings.json',0o600)
                    f.put(str(s.BASE/'remote_validation.py'),r+'/runner.py')
                j['deploymentStage'] = 'launching'
                save(j)
                remote(c,'nohup python3 '+r+'/runner.py >'+r+'/runner.log 2>&1 </dev/null &')
        deadline = time.monotonic()+j['options']['durationSeconds']+180
        while time.monotonic()<deadline:
            with connect(j,credentials) as c:
                with c.open_sftp() as f:
                    try:
                        with f.open(j['remoteRun']+'/status.json') as inp:
                            state = json.load(inp)
                    except FileNotFoundError:
                        state = {'status':'preparing'}
                if state['status'] == 'preparing' and reconcile_stale_validation(j, credentials, c):
                    return
            j['remoteStatus'] = state
            j['state'] = 'running' if state['status'] == 'running' else 'recovering'
            j['message'] = '正在监测远端状态；请在平台页面人工检查遥测，到时自动归档'
            if state['status'] in ('finished','failed','recovery_required'):
                j['rollbackRestored'] = state.get('rollbackRestored',False)
                j['state'] = 'completed' if state.get('originalConfigRestored') and state.get('serviceActive')=='active' and not state.get('error') else ('failed' if state.get('rollbackRestored') and state.get('serviceActive')=='active' else 'recovery_required')
                j['platformVerified'] = False
                j['verificationMode'] = 'manual'
                j['message'] = '运行结束，本轮已归档；请填写人工验收结果' if j['state']=='completed' else '远端任务需要核查，请查看恢复状态'
                j['finished'] = s.now()
                save(j)
                if j['state'] in ('completed','failed'):
                    (folder(jid)/'auth.dpapi').unlink(missing_ok=True)
                return
            save(j)
            time.sleep(10)
        raise TimeoutError()
    except Exception:
        if j.get('deploymentStage') == 'staging':
            j.update(state='failed', message='上传准备失败，尚未启动网关验证。可以重新准备验证包')
            (folder(jid)/'auth.dpapi').unlink(missing_ok=True)
        else:
            j.update(state='recovering', message='连接或监测中断；请点击恢复状态监测确认远端状态与配置恢复，勿重复部署')
        save(j)
    finally:
        ACTIVE.discard(jid)

def resume(jid):
    with LOCK:
        j = record(jid)
        if jid not in ACTIVE and j.get('remoteRun') and (folder(jid)/'auth.dpapi').exists():
            credentials = json.loads(s.crypt((folder(jid)/'auth.dpapi').read_bytes(),True))
            if reconcile_stale_validation(j, credentials):
                return record(jid)
            ACTIVE.add(jid)
            threading.Thread(target=worker,args=(jid,),daemon=True).start()
        return j

def stop(jid):
    j = record(jid)
    if j['state'] not in ('starting','running','recovering') or not j.get('remoteRun'):
        raise s.Problem('没有可停止的远程任务')
    credentials = json.loads(s.crypt((folder(jid)/'auth.dpapi').read_bytes(),True))
    with connect(j,credentials) as c:
        with c.open_sftp() as f:
            with f.open(j['remoteRun']+'/stop-request','w') as out:
                out.write('stop')
    return {'message':'已请求提前停止，远端脚本将恢复配置'}


def assessment(jid, body):
    with LOCK:
        j = record(jid)
        if not j.get('remoteStatus',{}).get('startedAtMs'): raise s.Problem('本轮尚未开始运行，不能填写验收记录')
        count = body.get('observedDevices')
        if type(count) is not int or not 0 <= count <= len(j['devices']):
            raise s.Problem('观测设备数须在 0 至本轮配置设备数之间')
        notes = body.get('notes', '')
        if not isinstance(notes, str) or len(notes) > 4000:
            raise s.Problem('验收说明最多 4000 字')
        measured = body.get('metrics',{})
        if not isinstance(measured,dict) or set(measured)-acceptance.METRICS.keys(): raise s.Problem('测量指标无效')
        import math
        for key,value in measured.items():
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value < 0 or ('Percent' in key and value > 100)):
                raise s.Problem('测量指标无效：'+key)
        if measured.get('connectedChannels') is not None and type(measured['connectedChannels']) is not int: raise s.Problem('连接成功数须为整数')
        try: measurement_window = acceptance.window(j)
        except s.Problem: measurement_window = {}
        j['measurementWindow'] = measurement_window
        j.update(observedDevices=count, assessmentNotes=notes, assessmentAt=s.now(), verificationMode='manual', measuredMetrics=measured, resultUpdatedAt=time.time_ns())
        j.setdefault('assessmentHistory',[]).append({'at':j['assessmentAt'],'manifestId':j.get('manifestId'), 'observedDevices':count,'notes':notes,'metrics':measured,'window':measurement_window})
        j['acceptanceResult'] = acceptance.verdict(j)
        save(j)
        return j
