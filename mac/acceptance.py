"""Execution manifests and explicit read-only telemetry acceptance for test rounds."""
import copy
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode
import workspace as s

ACTIVE = set()
LOCK = threading.RLock()
METRICS = {'connectedChannels':('minConnectedChannels', False), 'ratePerSecond':('minRatePerSecond', False), 'p95LatencyMs':('maxP95LatencyMs', True),
           'lossPercent':('maxLossPercent', True), 'errorPercent':('maxErrorPercent', True)}


def criteria(body):
    if not isinstance(body,dict): raise s.Problem('验收阈值格式无效')
    result = {}
    for key in ('minReportingPercent','minConnectedChannels','minRatePerSecond','maxP95LatencyMs','maxLossPercent','maxErrorPercent'):
        value = body.get(key, 100 if key == 'minReportingPercent' else None)
        if key == 'minReportingPercent' and value is None: raise s.Problem('请填写最低上报设备比例')
        if key == 'minConnectedChannels' and value is not None and type(value) is not int: raise s.Problem('连接数阈值须为整数')
        if value is not None:
            if type(value) not in (int,float) or not math.isfinite(value) or value < 0 or ('Percent' in key and value > 100):
                raise s.Problem('验收阈值无效：'+key)
        result[key] = value
    return result


def freeze(j, configs):
    manifest = {'projectId':j['projectId'], 'revision':j['revision'], 'fingerprint':j['fingerprint'],
                'sourceJobId':j.get('sourceJobId'), 'testRound':j.get('testRound'),
                'devices':[{k:v for k,v in device.items() if k != 'expectedKeys'} | {'expectedKeysHash':s.digest(device.get('expectedKeys',[]))} for device in j['devices']], 'options':copy.deepcopy(j['options']),
                'configHashes':{name:hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest() for name,cfg in configs.items()}}
    j['manifestId'] = s.digest(manifest)
    j['executionManifest'] = manifest
    j['assessmentHistory'] = []


def window(j, now_ms=None):
    start = j.get('remoteStatus',{}).get('startedAtMs')
    if not start: raise s.Problem('本轮尚未开始运行')
    start += j['options'].get('warmupSeconds',0)*1000
    planned_end = start + j['options'].get('sampleSeconds',j['options']['durationSeconds'])*1000
    end = min(now_ms if now_ms is not None else int(time.time()*1000),
              j.get('remoteStatus',{}).get('endedAtMs', planned_end), planned_end)
    if end <= start: raise s.Problem('尚在预热或提前停止，暂无正式采样窗口')
    return {'startTs':start, 'endTs':end, 'complete':end>=planned_end}


def telemetry_keys_of(member):
    """Use the outgoing telemetry name, not the gateway's internal Tag number."""
    return [str(point.get('customize_info') or point['tag']) for point in member['point_table']]


def query_keys_for(j, configs):
    expected_hashes = j.get('executionManifest',{}).get('configHashes')
    actual_hashes = {name:hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest()
                     for name,cfg in configs.items()}
    if expected_hashes != actual_hashes:
        raise s.Problem('部署文件已变化，无法按原执行清单核验遥测',409)
    devices_by_name = {device['name']:device for device in j['devices']}
    devices_by_type = {}
    for device in j['devices']:
        devices_by_type.setdefault(device['type'].lower(),[]).append(device)
    keys_by_id = {}
    for config in configs.values():
        for channel in config['corridors_zf']:
            for member in channel['server']['devices']:
                target = devices_by_name.get(member['deviceName'])
                if target is None:
                    # Older microgrid packages name the outgoing member C01.
                    category = re.sub(r'\d+(?:-\d+)?$', '', channel['alias_name']).lower()
                    candidates = devices_by_type.get(category,[])
                    if len(candidates) == 1 and len(channel['server']['devices']) == 1:
                        target = candidates[0]
                if target is not None:
                    keys_by_id[target['id']] = telemetry_keys_of(member)
    return keys_by_id


def stored_query_keys(j):
    """Use the frozen outgoing names without loading large encrypted gateway configs."""
    frozen = j.get('executionManifest',{}).get('devices',[])
    if len(frozen) != len(j['devices']):
        raise s.Problem('设备清单与执行清单不一致，无法核验遥测',409)
    result = {}
    for device, original in zip(j['devices'], frozen):
        keys = device.get('expectedKeys') or []
        if device['id'] != original['id'] or s.digest(keys) != original['expectedKeysHash']:
            raise s.Problem('设备测点与执行清单不一致，无法核验遥测',409)
        result[device['id']] = keys
    return result


def keys_for_check(j, jid):
    keys_by_id = stored_query_keys(j)
    legacy = [device for device in j['devices'] if device.get('expectedKeys') and
              re.fullmatch(r'(?:C\d+\.D\d+\.)?Tag\d+',device['expectedKeys'][0])]
    if legacy:
        import deployment as d
        recovered = query_keys_for(j,d.configs_for(jid))
        for device in legacy:
            if recovered.get(device['id']):
                keys_by_id[device['id']] = recovered[device['id']]
    return keys_by_id


def verdict(j):
    evidence = j.get('telemetryCheck',{})
    rules = j['options'].get('criteria',{})
    checks = []
    if evidence.get('state') == 'completed' and evidence.get('manifestId') == j.get('manifestId'):
        checks.append({'metric':'reportingPercent', 'value':evidence['reportingPercent'],
                       'passed':evidence['reportingPercent'] >= (rules.get('minReportingPercent') if rules.get('minReportingPercent') is not None else 100)})
    else:
        checks.append({'metric':'reportingPercent','value':None,'passed':None})
    measured = j.get('measuredMetrics',{})
    for key,(target,maximum) in METRICS.items():
        if rules.get(target) is not None:
            value = measured.get(key)
            checks.append({'metric':key,'value':value,'passed':None if value is None else (value <= rules[target] if maximum else value >= rules[target])})
    incomplete = (j.get('state') != 'completed' or j.get('remoteStatus',{}).get('stoppedEarly',False) or not evidence.get('window',{}).get('complete') or evidence.get('errors',0)>0 or
                  any(x['passed'] is None for x in checks) or (any(rules.get(target) is not None for target,_ in METRICS.values()) and not j.get('measurementWindow',{}).get('complete')))
    state = 'incomplete' if incomplete else 'failed' if any(x['passed'] is False for x in checks) else 'passed'
    return {'state':state,'checks':checks,'manifestId':j.get('manifestId'),
            'note':'遥测按平台存储时间戳核对；速率、延迟、丢失率、错误率为人工测量录入。不能证明数据仅来自本轮网关。'}


def start_check(jid, body):
    import deployment as d
    username, password = body.get('username'), body.get('password')
    if not isinstance(username,str) or not isinstance(password,str) or not username or not password:
        raise s.Problem('请输入目标平台租户管理员账号和密码')
    with LOCK, d.LOCK:
        if jid in ACTIVE: raise s.Problem('本轮遥测核验正在执行',409)
        j = d.record(jid)
        if not j.get('manifestId'): raise s.Problem('历史记录没有执行清单，请重新准备部署包')
        stored_query_keys(j)
        sampling = window(j)
        j['telemetryCheck'] = {'state':'running','checked':0,'total':len(j['devices']),
                               'window':sampling,'manifestId':j['manifestId'],'startedAt':s.now()}
        j['resultUpdatedAt'] = time.time_ns()
        j['platformVerified'] = False
        j['acceptanceResult'] = verdict(j)
        ACTIVE.add(jid)
        d.save(j)
        try:
            threading.Thread(target=worker,args=(jid,username,password,sampling),daemon=True).start()
        except Exception:
            ACTIVE.discard(jid)
            j['telemetryCheck'].update(state='failed',message='无法启动核验任务，请重试')
            d.save(j)
            raise s.Problem('无法启动核验任务，请重试')
        return j


def worker(jid, username, password, sampling):
    import deployment as d
    from app.platform.thingsboard_client import ThingsBoardClient
    j = d.record(jid)
    rows = []
    stage = 'keys'
    try:
        keys_by_id = keys_for_check(j, jid)
        # Bounded concurrency and one login; credentials remain in memory for this check only.
        stage = 'login'
        with ThingsBoardClient(j['platformUrl']) as client:
            client._client.follow_redirects = False
            client.login(username,password)
            stage = 'query'
            def inspect(device):
                result = {'id':device['id'],'name':device['name'],'status':'missing'}
                if not device['id']:
                    result['status'] = 'unknown'; return result
                try:
                    keys = keys_by_id.get(device['id']) or device.get('expectedKeys') or []
                    if not keys:
                        result['status'] = 'unknown'; return result
                    for offset in range(0,len(keys),64):
                        query = urlencode({'keys':','.join(keys[offset:offset+64]),'startTs':sampling['startTs'],
                                           'endTs':sampling['endTs'],'limit':1,'agg':'NONE','orderBy':'DESC'})
                        data = client._request('GET','/api/plugins/telemetry/DEVICE/'+quote(device['id'],safe='')+'/values/timeseries?'+query)
                        if not isinstance(data,dict): raise ValueError('invalid telemetry')
                        stamps = [v['ts'] for key in keys[offset:offset+64] for v in data.get(key,[])
                                  if isinstance(v,dict) and type(v.get('ts')) in (int,float) and sampling['startTs'] <= v['ts'] <= sampling['endTs']]
                        if stamps:
                            result.update(status='reported',lastTs=max(stamps)); break
                except Exception:
                    result['status'] = 'unknown'
                return result
            with ThreadPoolExecutor(max_workers=4) as executor:
                for result in executor.map(inspect,j['devices']):
                    rows.append(result)
                    if len(rows)%500 == 0:
                        with d.LOCK:
                            current=d.record(jid);current['telemetryCheck']['checked']=len(rows)
                            current['resultUpdatedAt']=time.time_ns();d.save(current)
        stage = 'save'
        with d.LOCK:
            current=d.record(jid)
            reported=sum(r['status']=='reported' for r in rows)
            errors=sum(r['status']=='unknown' for r in rows)
            current['telemetryCheck'].update(state='completed',checked=len(rows),reported=reported,errors=errors,
                missing=sum(r['status']=='missing' for r in rows),reportingPercent=100*reported/len(rows),
                missingDevices=[r for r in rows if r['status']=='missing'],unknownDevices=[r for r in rows if r['status']=='unknown'],finishedAt=s.now())
            current['platformVerified']=False  # Evidence of data presence is not provenance or full telemetry correctness.
            current['resultUpdatedAt']=time.time_ns();current['acceptanceResult']=verdict(current)
            history=current.setdefault('telemetryHistory',[]);history.append(dict(current['telemetryCheck']))
            d.save(current)
    except Exception as exc:
        if stage == 'keys':
            message = '读取或校验部署设备测点失败，请检查本地部署记录'
        elif stage == 'login':
            status = re.search(r'returned (\d{3})',str(exc))
            message = ('平台登录被拒绝（HTTP '+status[1]+'），请核对租户管理员账号和密码' if status and status[1] in ('401','403')
                       else '平台登录连接失败或响应异常，请核对平台地址和连接状态')
        elif stage == 'save':
            message = '遥测核验结果保存失败，请检查本地磁盘和工作台日志'
        else:
            message = '遥测查询任务失败，请检查平台连接后重试'
        with d.LOCK:
            current=d.record(jid);current['telemetryCheck'].update(state='failed',message=message,failureStage=stage)
            current['resultUpdatedAt']=time.time_ns();current['acceptanceResult']=verdict(current);d.save(current)
    finally:
        with LOCK: ACTIVE.discard(jid)
