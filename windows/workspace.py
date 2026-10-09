"""Independent phase-one workspace. No platform calls occur while importing/planning."""
from __future__ import annotations
import copy, ctypes, hashlib, json, os, re, shutil, threading, time, zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from gateway_engine import workbench as w
from gateway_engine import gateway_generator as g
import microgrid as mg

def engine(p): return mg if p.get("profile")=="microgrid" else w
def kinds_for(profile):
    if profile not in ("storage","microgrid"): raise Problem("工程类型无效")
    return mg.KINDS if profile=="microgrid" else KINDS

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("UNIFIED_DATA_DIR", BASE / "data")).resolve()
HISTORY = Path(os.environ.get("UNIFIED_HISTORY_DIR", BASE.parent / "启动包" / "快速造数与云边自动化测试平台" / "outputs")).resolve()
KINDS = ("PCS","BMS","GROUPBMS","CELL","CL","BOX")
ALIASES = {"pcs":"PCS","bms":"BMS","groupbms":"GROUPBMS","cell":"CELL","cl":"CL","strategy":"CL","策略":"CL","box":"BOX","箱变":"BOX"}
CANDIDATE = dict(zip(KINDS,(290,290,100,900,140,130)))
LOCK = threading.RLock()
w.DATA = DATA / "gateway"
for folder in ("projects","vault","jobs","plans","cloud"):
    (DATA/folder).mkdir(parents=True,exist_ok=True)

class Problem(ValueError):
    def __init__(self,message,status=422):
        super().__init__(message); self.status=status

def now(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def uid(): return uuid4().hex
def valid_id(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-f0-9]{32}",value): raise Problem("记录编号无效")
    return value
def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+"."+uid()+".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    try:
        for attempt in range(6):
            try:
                os.replace(temp,path)
                break
            except PermissionError:
                if attempt==5: raise
                time.sleep(.02*(attempt+1))
    finally:
        temp.unlink(missing_ok=True)
def read(path):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError: raise Problem("记录不存在",404)
    except (ValueError,UnicodeError): raise Problem("记录损坏，请检查来源文件")
def project_path(pid): return DATA/"projects"/(valid_id(pid)+".json")
def get_project(pid):
    p=read(project_path(pid))
    if p.get("deletedAt"): raise Problem("工程已删除，请先从回收站恢复",404)
    return p
def save_project(p):
    atomic(DATA/"projects"/p["id"]/("v%d.json"%p["revision"]),p)
    atomic(project_path(p["id"]),p)

PORTABLE_SECRET_MAGIC = b'CEW1'


def portable_key(create=False):
    path = Path(os.environ.get('UNIFIED_PORTABLE_KEY_FILE', DATA/'portable-key.bin'))
    if create and not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor,'wb') as output:
                output.write(os.urandom(32))
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        raise Problem('迁移密钥文件缺失，请将随迁移包交付的密钥放入 data/portable-key.bin')
    if len(key)!=32: raise Problem('迁移密钥文件无效')
    return key


def crypt(blob,decrypt=False):
    """Windows DPAPI for existing files; AES-GCM for portable macOS data."""
    if decrypt and blob.startswith(PORTABLE_SECRET_MAGIC):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            return AESGCM(portable_key()).decrypt(blob[4:16],blob[16:],None)
        except Exception:
            raise Problem('迁移数据解密失败，请核对密钥文件')
    if os.name!='nt':
        if decrypt: raise Problem('此记录仍使用 Windows DPAPI，请在原 Windows 账号下重新导出迁移包')
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce=os.urandom(12)
        return PORTABLE_SECRET_MAGIC+nonce+AESGCM(portable_key(create=True)).encrypt(nonce,blob,None)
    from ctypes import wintypes
    class BLOB(ctypes.Structure):
        _fields_=[("cbData",wintypes.DWORD),("pbData",ctypes.POINTER(ctypes.c_byte))]
    buf=ctypes.create_string_buffer(blob)
    src=BLOB(len(blob),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte))); dst=BLOB()
    lib=ctypes.WinDLL("crypt32",use_last_error=True)
    f=lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    f.argtypes=[ctypes.POINTER(BLOB),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(BLOB)]
    f.restype=wintypes.BOOL
    if not f(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dst)):
        raise Problem("本机凭据保护失败，请使用创建工程的 Windows 账号")
    try: return ctypes.string_at(dst.pbData,dst.cbData)
    finally:
        kernel=ctypes.WinDLL("kernel32"); kernel.LocalFree.argtypes=[ctypes.c_void_p]; kernel.LocalFree(ctypes.cast(dst.pbData,ctypes.c_void_p))
def put_secret(value):
    ref=uid(); (DATA/"vault"/(ref+".bin")).write_bytes(crypt(json.dumps(value).encode()))
    return ref
def get_secret(ref):
    try: return json.loads(crypt((DATA/"vault"/(valid_id(ref)+".bin")).read_bytes(),True))
    except FileNotFoundError: raise Problem("凭据文件缺失，请重新导入历史记录")
def kind_of(d, profile="storage"):
    match=re.search(r"(?:^|;)\s*template=([^;]+)",d.get("notes",""))
    value=(match[1] if match else d.get("business_type","")).strip().lower()
    if profile == "microgrid":
        if value.upper() not in mg.KINDS: raise Problem("微电网设备类型必须是 PCS、ESS、INV、BOX")
        return value.upper()
    if value not in ALIASES: raise Problem("设备业务类型不明确，记录需包含 template 来源")
    return ALIASES[value]
def history_path(run_id):
    if not isinstance(run_id,str) or not re.fullmatch(r"run-[A-Za-z0-9-]{1,100}",run_id): raise Problem("历史记录编号无效")
    candidate=(HISTORY/run_id/"entities.json").resolve()
    if not candidate.is_relative_to(HISTORY): raise Problem("历史路径超出允许范围")
    return candidate
def history_list():
    items=[]
    if HISTORY.is_dir():
        for folder in sorted(HISTORY.glob("run-*"),reverse=True):
            try:
                d=read(history_path(folder.name))
                items.append({"runId":folder.name,"status":d.get("status"),"devices":len(d.get("devices",[])),"assets":len(d.get("assets",[])),"created":d.get("startedAt")})
            except (Problem,OSError): continue
    return items

def source_manual_estimate(project):
    source=project.get('sourceRunId')
    if not source: return None
    try:
        history_path(source)  # Validate the run ID before using it in a local path.
        protected=DATA/'cloud'/source/'entities.dpapi'
        if protected.is_file():
            return json.loads(crypt(protected.read_bytes(),True)).get('manualEstimate')
        return read(history_path(source)).get('manualEstimate')
    except (Problem,OSError,ValueError):
        return None
def defaults():
    return {"maxProjectPoints":100000,"maxForwardTags":6000,"forwardMode":"direct","mqttHost":"","mqttPort":1883,"mqttQos":1,"mqttPub":"v1/devices/me/telemetry","mqttSub":"v1/devices/me/rpc/request/+","changePeriod":1,"pubTimer":300,"projectPrefix":"unified","strategy":"balanced","allowFill":False,"templateDbPath":str(BASE/"templates"/"db.xml"),"templateBmsCsvPath":str(BASE/"templates"/"C219_D01.csv"),"alarmRules":[],"sharedDevicePrefix":"","mqttSn":"TN001"}
def validate_gateway(c):
    out={k:copy.deepcopy(c.get(k,v)) for k,v in defaults().items()}
    for key,lo,hi in (("maxProjectPoints",1,20000000),("maxForwardTags",1,20000000),("mqttPort",1,65535),("mqttQos",0,2),("changePeriod",0,100000000),("pubTimer",0,100000000)):
        try: out[key]=w.integer(out[key],key,lo,hi)
        except ValueError as e: raise Problem(str(e))
    if out["forwardMode"] not in ("direct","aggregate"): raise Problem("转发模式无效")
    if out["strategy"] not in ("balanced","compact"): raise Problem("分配方式无效")
    if type(out["allowFill"]) is not bool: raise Problem("占位补点设置无效")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}",str(out["projectPrefix"])): raise Problem("工程前缀须以字母开头，最多40字符")
    for k in ("mqttHost","mqttPub","mqttSub"):
        if not isinstance(out[k],str) or len(out[k])>512 or any(ord(x)<32 for x in out[k]): raise Problem("MQTT 参数格式无效")
    for key in ('templateDbPath','templateBmsCsvPath','mqttSn','sharedDevicePrefix'):
        if not isinstance(out[key],str) or len(out[key])>2048 or any(ord(x)<32 for x in out[key]): raise Problem('模板或通道设备参数格式无效')
    if out['sharedDevicePrefix'] and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}',out['sharedDevicePrefix']): raise Problem('通道设备前缀须以字母开头，仅支持字母数字下划线短横线')
    if not isinstance(out['alarmRules'],list) or any(not isinstance(r,dict) for r in out['alarmRules']): raise Problem('告警测点规则格式无效')
    g.alarm_rules(out)
    return out

def new_project(name="新工程", profile="storage"):
    kinds_for(profile)
    p = {"id":uid(),"revision":1,"schemaVersion":1,"name":str(name).strip()[:100] or "新工程","created":now(),"updated":now(),"sourceRunId":None,"sourceHash":None,"sourceStatus":None,"bindings":[],"relations":[],"types":[{"type":k,"total":0,"points":None,"fastPercent":20,"fastMs":1000,"slowMs":86400000} for k in KINDS],"gateway":defaults(),"pointsConfirmed":False,"mappingIssues":[],"cloud":{"base_url":"","device_name_prefix":"test-","asset_template":"公司,1,Company\n电站,1,powerStation\n储能单元,1,energyStationUnit","asset_name_prefix":"test-","edge_mode":"none","edge_names":"","edge_count":0,"enable_rule_chain_creation":False}}

    p["profile"] = profile
    if profile == "microgrid":
        p["types"] = [{"type":k,"total":v[0],"points":v[1],"fastPoints":v[2],"fastPercent":15,"fastMs":1000,"slowMs":86400000} for k,v in mg.PRESET.items()]
        p["microgrid"] = {"stationCount":None,"pointSource":"synthetic"}
        p["gateway"]["projectPrefix"] = "microgrid"
        p["cloud"].update(device_name_prefix="mg-",asset_name_prefix="mg-",asset_template="")
    return p

def credentials_of(d,doc):
    cred=d.get("raw",{}).get("credentials",{})
    typ=cred.get("credentialsType","MQTT_BASIC")
    if typ not in ("MQTT_BASIC","ACCESS_TOKEN"): raise Problem("存在不支持的设备凭据类型")
    client=str(d.get("mqtt_client_id") or "")
    user=str(d.get("mqtt_username") or "")
    secret=str(d.get("mqtt_password_or_token") or "")
    if typ=="ACCESS_TOKEN":
        user=secret or str(cred.get("credentialsId") or ""); secret=""
    if not (client or user): raise Problem("设备凭据缺失，请检查历史记录")
    if "***" in secret or "***" in user or "***" in client: raise Problem("历史记录中的凭据已脱敏，不能用于运行工程")
    # When the platform does not constrain clientId, use a stable per-device connection id.
    client=client or "unified-"+hashlib.sha256(str(d["id"]).encode()).hexdigest()[:24]
    return {"client_id":client,"user_name":user,"pass_word":secret}
def build_bindings(doc, profile="storage"):
    kinds=kinds_for(profile)
    devices=doc.get("devices",[])
    if not devices or len(devices)>100000: raise Problem("设备清单为空或超过10万台")
    ids=[str(d.get("id","")) for d in devices]; names=[str(d.get("name","")) for d in devices]
    if any(not i for i in ids+names) or len(set(ids))!=len(ids) or len(set(names))!=len(names): raise Problem("设备 ID/名称缺失或重复")
    typed={k:[] for k in kinds}
    for d in devices: typed[kind_of(d,profile)].append(d)
    for k in kinds: typed[k].sort(key=lambda d:(int(re.search(r"\d+$",d["name"])[0]) if re.search(r"\d+$",d["name"]) else 0,d["name"]))
    issues=[]; relations=[{"from":r.get("from_id"),"to":r.get("to_id")} for r in doc.get("relations",[]) if r.get("from_entity_type")=="DEVICE" and r.get("to_entity_type")=="DEVICE"]
    pairs={(r["from"],r["to"]) for r in relations}
    if profile == "storage" and any(typed[k] for k in ("BMS","GROUPBMS","CELL")):
        if len({len(typed[k]) for k in ("BMS","GROUPBMS","CELL")})!=1: issues.append("BMS、GROUPBMS、CELL 数量必须相同")
        else:
            groups={d["id"]:d for d in typed["GROUPBMS"]};cells={d["id"]:d for d in typed["CELL"]}
            ordered_g=[];ordered_c=[]
            for b in typed["BMS"]:
                gs=[groups[t] for f,t in pairs if f==b["id"] and t in groups]
                cs=[cells[t] for f,t in pairs if len(gs)==1 and f==gs[0]["id"] and t in cells]
                if len(gs)!=1 or len(cs)!=1: issues.append("BMS组合关联缺失或不唯一；请在设备映射中手动确认组合"); break
                ordered_g+=gs;ordered_c+=cs
            if not issues and (len({d["id"] for d in ordered_g})!=len(ordered_g) or len({d["id"] for d in ordered_c})!=len(ordered_c)): issues.append("派生设备被多个BMS组合重复使用")
            if not issues: typed["GROUPBMS"]=ordered_g;typed["CELL"]=ordered_c
    bindings=[]
    for k in kinds:
        for i,d in enumerate(typed[k],1):
            bindings.append({"logicalId":uid(),"platformId":d["id"],"platformName":d["name"],"businessType":k,"platformType":d.get("type","default"),"collectAlias":k.lower()+str(i),"stackIndex":i if k in ("BMS","GROUPBMS","CELL") else None,"credentialRef":put_secret(credentials_of(d,doc)),"credentialStatus":"imported_unverified"})
    return bindings,relations,issues
def import_doc(doc,source,source_hash,profile="storage"):
    p=new_project("历史工程 · "+source[-6:],profile)
    bindings,relations,issues=build_bindings(doc,profile)
    p.update(sourceRunId=source,sourceHash=source_hash,sourceStatus=doc.get("status"),bindings=bindings,relations=relations,mappingIssues=issues)
    counts=Counter(b["businessType"] for b in bindings)
    for row in p["types"]: row["total"]=counts[row["type"]]
    c=doc.get("configSnapshot",{})
    p["gateway"]["mqttHost"]=str(c.get("mqtt_host") or "")
    p["gateway"]["mqttPort"]=int(c.get("mqtt_port") or 1883)
    p["environmentHash"]=digest({"base":c.get("base_url"),"tenant":doc.get("tenant",{}),"adminId":(doc.get("tenantAdmin") or {}).get("id")})
    save_project(p); return p
def import_run(run_id,profile="storage"):
    file=history_path(run_id); doc=read(file)
    return import_doc(doc,run_id,hashlib.sha256(file.read_bytes()).hexdigest(),profile)
def active_bindings(p):
    """Select this generation's devices without discarding the reusable device pool."""
    counts = {row['type']: row['total'] for row in p['types']}
    if p.get('profile', 'storage') == 'storage':
        if len({counts.get(kind, 0) for kind in ('BMS', 'GROUPBMS', 'CELL')}) != 1:
            raise Problem('BMS、GROUPBMS、CELL 共用采集通道，本次生成数量必须相同')
    selected = []
    for kind, count in counts.items():
        pool = [b for b in p['bindings'] if b['businessType'] == kind]
        if count > len(pool):
            raise Problem(f'{kind}本次需要{count}台，已绑定设备池只有{len(pool)}台；请先导入足够的已有设备')
        by_alias = {b['collectAlias']: b for b in pool}
        for index in range(1, count + 1):
            alias = kind.lower() + str(index)
            if alias not in by_alias:
                raise Problem(f'{kind}采集编号不连续，请检查设备映射')
            selected.append(by_alias[alias])
    return selected

def update(pid,data):
    with LOCK:
        p=get_project(pid)
        if data.get("revision")!=p["revision"]: raise Problem("配置已更新，请重新打开后保存",409)
        if any(j.get("state")=="running" and j.get("projectId")==pid for j in jobs()): raise Problem("工程任务执行中，请完成后修改",409)
        types=data.get("types")
        profile=p.get("profile","storage"); kinds=kinds_for(profile)
        if data.get("profile",profile)!=profile: raise Problem("已有工程不能切换类型，请新建工程")
        if not isinstance(types,list) or any(not isinstance(r,dict) for r in types) or {r.get("type") for r in types}!=set(kinds) or len(types)!=len(kinds): raise Problem("设备类型与当前工程方案不一致")
        rows=[]
        for k in kinds:
            r=next(r for r in types if r["type"]==k)
            row={"type":k}
            for field,lo,hi in (("total",0,100000),("fastPercent",0,100),("fastMs",1,86400000),("slowMs",1,604800000)):
                try: row[field]=w.integer(r.get(field),field,lo,hi)
                except ValueError as e: raise Problem(str(e))
            row["points"]=None if r.get("points") in (None,"") else w.integer(r["points"],"单设备点数",1,20000000)
            if profile=="microgrid":
                row["fastPoints"]=w.integer(r.get("fastPoints"),"单设备秒级点数",0,row["points"] or 0)
            rows.append(row)
        if p.get('testRound') and any(r['total']!=sum(b['businessType']==r['type'] for b in p['bindings']) for r in rows): raise Problem('轮次设备选择已固定，请新建下一轮调整数量')
        if p["bindings"] and not p.get('templateMode'): active_bindings({**p, "types":rows})
        if sum(r["total"] for r in rows)>100000 or sum(r["total"]*(r["points"] or 0) for r in rows)>20000000: raise Problem("超过10万设备或2000万点上限")
        if type(data.get("pointsConfirmed",False)) is not bool: raise Problem("确认状态无效")
        p.update(name=str(data.get("name",p["name"])).strip()[:100] or "未命名工程",types=rows,pointsConfirmed=data.get("pointsConfirmed",False),gateway=validate_gateway(data.get("gateway",{})))
        if p.get('templateMode'):
            counts = {row['type']:row['total'] for row in rows}
            p['bindings'] = [b for b in p['bindings'] if b.get('credentialStatus') != 'template_unverified'
                             or int(b['collectAlias'][len(b['businessType']):]) <= counts[b['businessType']]]
        cloud=data.get("cloud",p["cloud"])
        allowed=("base_url","device_name_prefix","asset_name_prefix","asset_template","edge_mode","edge_names","edge_count","enable_rule_chain_creation")
        p["cloud"]={k:cloud.get(k,p["cloud"].get(k)) for k in allowed}
        if profile=="microgrid":
            count=data.get("microgrid",p["microgrid"]).get("stationCount")
            count=None if count in (None,"") else w.integer(count,"电站数量",1,10000)
            if p["bindings"] and count!=p["microgrid"]["stationCount"]: raise Problem("已绑定工程不能修改电站数量")
            p["microgrid"]={"stationCount":count,"pointSource":"synthetic"}
            p["cloud"]["asset_template"]="电站,"+str(count)+",powerStation" if count else ""
        p["revision"]+=1;p["updated"]=now();save_project(p);return p
def set_stacks(pid,revision,mapping):
    with LOCK:
        p=get_project(pid)
        if p["revision"]!=revision: raise Problem("版本已变化",409)
        if any(j.get("state")=="running" and j.get("projectId")==pid for j in jobs()): raise Problem("任务运行中不能修改组合",409)
        if p.get("profile")=="microgrid": raise Problem("微电网不使用BMS组合绑定")
        index={b["logicalId"]:b for b in p["bindings"]}
        expected={b["logicalId"] for b in p["bindings"] if b["businessType"] in ("BMS","GROUPBMS","CELL")}
        used=[]
        for i,row in enumerate(mapping,1):
            if set(row)!=set(("BMS","GROUPBMS","CELL")): raise Problem("组合必须包含三类设备")
            for kind,logical in row.items():
                if logical not in index or index[logical]["businessType"]!=kind: raise Problem("组合设备类型或身份错误")
                used.append(logical);index[logical]["stackIndex"]=i;index[logical]["collectAlias"]=kind.lower()+str(i)
        if len(set(used))!=len(used) or set(used)!=expected: raise Problem("组合必须完整且不重复地覆盖三类设备")
        p["mappingIssues"]=[];p["stackSource"]="user_confirmed";p["revision"]+=1;save_project(p);return p
def project_summary(p):
    result={k:p[k] for k in ('id','revision','name','created','updated','sourceRunId','pointsConfirmed')}
    result.update(devices=sum(r['total'] for r in p['types']),boundDevices=len(p['bindings']),profile=p.get('profile','storage'))
    if p.get('testRound'): result['testRound']={k:p['testRound'][k] for k in ('poolId','poolRevision','poolName','poolDevices')}
    return result
def projects(deleted=False):
    records=[read(f) for f in (DATA/"projects").glob("*.json")]
    return sorted([project_summary(p) for p in records if bool(p.get("deletedAt"))==deleted],key=lambda p:p["updated"],reverse=True)

def set_project_deleted(pid,revision,deleted):
    with LOCK:
        p=read(project_path(pid))
        if p["revision"]!=revision: raise Problem("工程已更新，请刷新列表后重试",409)
        if any(j.get("projectId")==pid and j.get("state")=="running" for j in jobs()): raise Problem("工程任务正在执行，请完成后再删除",409)
        if bool(p.get("deletedAt"))==deleted: return project_summary(p)
        if deleted: p["deletedAt"]=now()
        else: p.pop("deletedAt",None)
        p["revision"]+=1;p["updated"]=now();save_project(p)
        audit("delete_project" if deleted else "restore_project",pid,"completed")
        return project_summary(p)

def _secret_refs(value):
    if isinstance(value,dict):
        refs={value[key] for key in ('credentialRef','planSecret') if isinstance(value.get(key),str) and re.fullmatch(r'[a-f0-9]{32}',value[key])}
        for child in value.values(): refs.update(_secret_refs(child))
        return refs
    if isinstance(value,list):
        refs=set()
        for child in value: refs.update(_secret_refs(child))
        return refs
    return set()

def _local_delete(path):
    root=DATA.resolve()
    target=path.resolve()
    if target==root or not target.is_relative_to(root) or path.is_symlink():
        raise Problem('删除路径超出工作台数据目录或为符号链接')
    if path.is_dir(): shutil.rmtree(path)
    else: path.unlink(missing_ok=True)

def permanently_delete_project(pid,revision,confirm_name):
    """Remove one trashed local project and its private artifacts; never touch remote resources."""
    with LOCK:
        p=read(project_path(pid))
        if not p.get('deletedAt'): raise Problem('只能永久删除回收站中的工程',409)
        if p['revision']!=revision: raise Problem('工程已更新，请刷新回收站后重试',409)
        if confirm_name!=p['name']: raise Problem('请输入完整工程名称确认永久删除',409)
        all_projects=[read(path) for path in (DATA/'projects').glob('*.json')]
        if any(other['id']!=pid and (other.get('testRound') or {}).get('poolId')==pid for other in all_projects):
            raise Problem('其他测试轮次仍依赖该设备池，请先处理关联轮次',409)
        project_jobs=[job for job in jobs() if job.get('projectId')==pid]
        if any(job.get('state')=='running' for job in project_jobs):
            raise Problem('工程任务仍在运行，暂不能永久删除',409)
        plans=[read(path) for path in (DATA/'plans').glob('*.json')]
        own_plans=[plan for plan in plans if plan.get('projectId')==pid]
        deployments=[read(path) for path in (DATA/'deployments').glob('*/record.json')]
        own_deployments=[item for item in deployments if item.get('projectId')==pid]
        if any(item.get('state') in ('starting','running','recovering','recovery_required') or
               (DATA/'deployments'/valid_id(item['id'])/'auth.dpapi').exists() for item in own_deployments):
            raise Problem('仍有部署任务需要完成或恢复，暂不能永久删除',409)
        own_job_ids={job['id'] for job in project_jobs}
        own_deployment_ids={item['id'] for item in own_deployments}
        if any(item.get('projectId')!=pid and
               (item.get('sourceJobId') in own_job_ids or item.get('options',{}).get('previousDeploymentId') in own_deployment_ids)
               for item in deployments):
            raise Problem('其他工程仍引用该工程的生成或部署记录，请先处理关联记录',409)
        snapshots=[read(path) for path in (DATA/'projects'/pid).glob('v*.json')]
        owned_refs=set().union(*(_secret_refs(doc) for doc in [p,*snapshots,*own_plans]))
        other_projects=[other for other in all_projects if other['id']!=pid]
        other_snapshots=[read(path) for other in other_projects for path in (DATA/'projects'/valid_id(other['id'])).glob('v*.json')]
        other_refs=set().union(*(_secret_refs(doc) for doc in [*other_projects,*other_snapshots,
                                                                *(plan for plan in plans if plan.get('projectId')!=pid)]))
        targets=[]
        for job in project_jobs:
            jid=valid_id(job['id'])
            targets.extend((DATA/'packages'/jid, DATA/'cloud'/('run-'+jid), job_path(jid)))
        for item in own_deployments: targets.append(DATA/'deployments'/valid_id(item['id']))
        for plan in own_plans: targets.append(DATA/'plans'/(valid_id(plan['id'])+'.json'))
        targets.extend((DATA/'projects'/pid,project_path(pid)))
        targets.extend(DATA/'vault'/(ref+'.bin') for ref in owned_refs-other_refs)
        root=DATA.resolve()
        if any(path.resolve()==root or not path.resolve().is_relative_to(root) or path.is_symlink() for path in targets):
            raise Problem('删除路径超出工作台数据目录或为符号链接')
        for path in targets: _local_delete(path)
        audit('permanently_delete_project',pid,'completed')
        return {'id':pid,'deleted':True}
def jobs():
    return sorted([read(f) for f in (DATA/"jobs").glob("*.json")],key=lambda j:j["created"],reverse=True)
def job_path(jid): return DATA/"jobs"/(valid_id(jid)+".json")
def note_job(j): atomic(job_path(j["id"]),j)
def audit(action,pid,result):
    with (DATA/"audit.jsonl").open("a",encoding="utf-8") as f: f.write(json.dumps({"time":now(),"action":action,"projectId":pid,"result":result},ensure_ascii=False)+"\n")
def safe_error(exc):
    # Never persist exception messages from external systems; local validation is controlled.
    return str(exc) if isinstance(exc,Problem) else "处理失败，请检查模板或运行环境；原始异常未写入公开日志"

def cloud_failure(doc):
    """Only the known pre-write connection precheck permits retry. Never infer from counts alone."""
    error=str(doc.get("error", ""))
    if doc.get('configSnapshot',{}).get('reuse_existing_devices') is True:
        missing = re.fullmatch(r'已有设备未找到：([A-Za-z0-9_-]{1,100})；请核对设备前缀和数量，不会自动补建', error)
        credential = re.fullmatch(r'设备 ([A-Za-z0-9_-]{1,100}) 的(凭据类型不支持|MQTT 凭据缺失或已脱敏)', error)
        if missing:
            reason = '未找到设备 '+missing.group(1)+'。请核对设备前缀和各类型数量；当前按从 1 开始的连续编号匹配，不会跳号或自动补建。'
        elif credential:
            reason = '设备 '+credential.group(1)+' 的'+credential.group(2)+'。请检查该设备的 MQTT 凭据和账号读取权限。'
        elif error == '已有设备ID或MQTT客户端ID重复':
            reason = '查询结果包含重复的设备 ID 或 MQTT 客户端 ID，请检查平台设备凭据。'
        elif error.startswith('云平台连接预检失败。'):
            reason = '无法连接目标平台，请检查平台地址、端口及网络连接。'
        else:
            reason = '设备、凭据或关系查询校验未完成，请检查平台地址、租户账号权限及设备映射。'
        return {'retrySafe':False,'failureStage':'reuse_query','message':'查询并绑定已有设备失败：'+reason+' 本次为只读查询，未创建或修改平台资源。请查看任务记录核对本地绑定状态。'}
    no_resources=not any(doc.get(k) for k in ("tenant","tenantAdmin","assets","devices","relations","edges","ruleChains","edgeAssignments"))
    retry=doc.get("status")=="FAILED" and no_resources and error.startswith("云平台连接预检失败。")
    if retry:
        from urllib.parse import urlsplit
        target=urlsplit(str(doc.get("configSnapshot",{}).get("base_url", "")))
        host=target.hostname or "未填写地址"
        port=target.port or (443 if target.scheme=="https" else 80)
        reason="连接被拒绝" if "10061" in error else "无法访问"
        return {"retrySafe":True,"failureStage":"connection_precheck","message":f"平台连接预检失败：{host}:{port} {reason}。尚未创建资源；请在平台与边缘修正地址/端口、保存后重新提交。"}
    return {"retrySafe":False,"failureStage":"unknown_or_after_precheck","message":"平台创建未完整成功；可能存在部分资源，请核对执行记录后处理，不能直接重复提交"}
def continue_with_templates(pid, revision):
    """Prepare local identities only; never create platform resources."""
    with LOCK:
        p = get_project(pid)
        if p['revision'] != revision: raise Problem('配置已变化，请重新保存后继续',409)
        if any(j.get('projectId')==pid and j['state']=='running' for j in jobs()): raise Problem('查询正在执行，请等待结果',409)
        if not p['pointsConfirmed']: raise Problem('请先确认设备与测点')
        prefix = p['cloud']['device_name_prefix']
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}',prefix): raise Problem('请填写有效设备前缀')
        by_alias = {b['collectAlias']:b for b in p['bindings']}
        for row in p['types']:
            for i in range(1,row['total']+1):
                alias = row['type'].lower()+str(i)
                if alias in by_alias: continue
                name = prefix+alias
                p['bindings'].append({'logicalId':uid(),'platformId':'','platformName':name,
                    'businessType':row['type'],'platformType':'default','collectAlias':alias,
                    'stackIndex':i if row['type'] in ('BMS','GROUPBMS','CELL') else None,
                    'credentialRef':put_secret({'client_id':name,'user_name':name,'pass_word':name}),
                    'credentialStatus':'template_unverified'})
        needed_shared = shared_device_specs(p)
        needed_names = {item['platformName'] for item in needed_shared}
        p['sharedBindings'] = [b for b in p.get('sharedBindings',[]) if b.get('credentialStatus') != 'template_unverified'
                               or b['platformName'] in needed_names]
        for item in needed_shared:
            url=p['cloud']['base_url'].rstrip('/')
            if any(b['platformName']==item['platformName'] and b.get('platformUrl')==url and b.get('credentialRef') and not b.get('credentialsPending') for b in p.get('sharedBindings',[])): continue
            name=item['platformName']
            p.setdefault('sharedBindings',[])
            p['sharedBindings']=[b for b in p['sharedBindings'] if not (b['platformName']==name and b.get('platformUrl')==url)]
            p['sharedBindings'].append({**item,'platformId':'','platformUrl':url,'credentialsPending':False,
                'credentialRef':put_secret({'client_id':name,'user_name':name,'pass_word':name}),'credentialStatus':'template_unverified'})
        p['templateMode']=any(b.get('credentialStatus')=='template_unverified' for b in p['bindings']+p.get('sharedBindings',[])) or bool(p['mappingIssues'])
        if p['mappingIssues']:
            p['templateMappingWarnings']=p['mappingIssues'];p['mappingIssues']=[]
        p['sourceStatus']='TEMPLATE' if p['templateMode'] else 'SUCCESS'
        p['revision']+=1;p['updated']=now();save_project(p)
        return p


def payload_for(p, require_binding=True):
    if not p["pointsConfirmed"]: raise Problem("请填写并确认每类点数和采集周期")
    if require_binding and not p["bindings"]: raise Problem("请先创建设备或导入历史记录，获取实际设备身份")
    if require_binding and p["sourceStatus"] not in ("SUCCESS","TEMPLATE"): raise Problem("请先查询设备或继续生成本地模板")
    if p["mappingIssues"]: raise Problem("；".join(p["mappingIssues"]))
    if not p["gateway"]["mqttHost"].strip(): raise Problem("请填写 MQTT 主机")
    config={**copy.deepcopy(w.DEFAULTS),**defaults(),**p["gateway"],"binding":"stack","platformDevicePrefix":"","identityMap":{},"deviceNameMap":{}}
    for b in active_bindings(p) if require_binding else []: config["identityMap"][b["collectAlias"]]=get_secret(b["credentialRef"])
    for b in active_bindings(p) if require_binding else []: config['deviceNameMap'][b['collectAlias']]=b['platformName']
    if config['forwardMode']=='aggregate' and require_binding:
        config['identityMap']={}
        pool={b['platformName']:b for b in p.get('sharedBindings',[]) if b.get('platformUrl')==p['cloud']['base_url'].rstrip('/')}
        for item in shared_device_specs(p):
            binding=pool.get(item['platformName'])
            if not binding or not binding.get('credentialRef') or binding.get('credentialsPending'):
                raise Problem('请先在平台设备准备中补齐共享通道设备：'+item['platformName'])
            config['identityMap'][item['collectAlias']]=get_secret(binding['credentialRef'])
    if not require_binding: config.pop("identityMap",None)
    ids=[v["client_id"] for v in config.get("identityMap",{}).values()]
    if len(set(ids))!=len(ids): raise Problem("设备凭据中的 MQTT 客户端 ID 重复")
    rows=[]
    for r in p["types"]:
        if r["total"] and not r["points"]: raise Problem(r["type"]+" 单设备点数缺失")
        n=r["points"] or 1;fast=r["fastPoints"] if p.get("profile")=="microgrid" else n*r["fastPercent"]//100
        rules=(str(r["fastMs"])+":"+str(fast)+"," if fast else "")+str(r["slowMs"])+":*"
        rows.append({"type":r["type"],"prefix":r["type"].lower(),"total":r["total"],"points":n,"intervalRules":rules})
    if p.get("profile")=="microgrid":
        config["binding"]="independent"
        for source,row in zip(p["types"],rows): row.update(fastPoints=source["fastPoints"],fastMs=source["fastMs"])
    return {"name":p["name"],"config":config,"types":rows}
def prepare(pid,revision):
    with LOCK:
        p=get_project(pid)
        if p["revision"]!=revision: raise Problem("请保存当前配置后重新计算",409)
        try: plan,planned,resolved=engine(p).prepare(payload_for(p))
        except ValueError as e: raise Problem(str(e))
        # Engine snapshot contains secrets: persist only DPAPI encrypted plan input.
        record={"id":uid(),"projectId":pid,"revision":revision,"fingerprint":digest(p),"enginePlanId":plan["planId"],"planSecret":put_secret(plan["snapshot"]),"created":now()}
        record["public"]={"planId":record["id"],"revision":revision,"summary":plan["summary"],"projects":plan["projects"],"templates":[{**{k:v for k,v in t.items() if k!="sample"},"sample":t.get("sample",[])[:100]} for t in plan["templates"]],"sources":plan["sources"],"filesValid":False,"platformVerified":False,"credentialsStatus":"imported_unverified","mapping":[{"logicalId":b["logicalId"],"collectAlias":b["collectAlias"],"platformName":b["platformName"],"platformId":b["platformId"],"type":b["businessType"]} for b in p["bindings"]]}
        record['public']['templateMode']=p.get('templateMode',False)
        if p.get('templateMode'): record['public']['credentialsStatus']='template_unverified'
        if p.get("profile")=="microgrid": record["public"].update(profile="microgrid",stations=mg.station_plan(p["types"],p["microgrid"]["stationCount"]))
        record['public']['testRound']=copy.deepcopy(p.get('testRound'))
        atomic(DATA/"plans"/(record["id"]+".json"),record);audit("plan",pid,"ready");return record["public"]
def forward_members(project,p):
    bindings={b['collectAlias']:b for b in active_bindings(p)}
    names={b['platformName']:b for b in bindings.values()}
    grouped=p['gateway']['forwardMode']=='aggregate'
    for channel in project['corridors_zf']:
        for device in channel['server']['devices']:
            b=names.get(device.get('deviceName')) if grouped else bindings.get(channel['alias_name'])
            if b is None: raise Problem('转发设备身份缺失')
            yield channel,device,b

def shared_credentials(p):
    config=payload_for(p)['config']
    result={row['type']:set() for row in p['types']}
    for item in shared_device_specs(p):
        cred=config['identityMap'][item['collectAlias']]
        result[item['businessType']].add(tuple(cred[k] for k in ('client_id','user_name','pass_word')))
    return result

def verify_bindings(project,p,seen,credential_sets=None):
    grouped=p['gateway']['forwardMode']=='aggregate'
    channels={}
    for channel,device,b in forward_members(project,p):
        alias=b['collectAlias']
        if alias in seen: raise Problem('转发设备绑定重复')
        seen.add(alias)
        channels.setdefault(channel['alias_name'],(channel,b))
        if not grouped:
            cred=get_secret(b['credentialRef'])
            if any(channel['server'].get(k)!=v for k,v in cred.items()): raise Problem('转发凭据与实际设备不一致')
    if grouped:
        # The original tool numbers connection credentials per type, across projects.
        allowed=credential_sets if credential_sets is not None else shared_credentials(p)
        for channel,b in channels.values():
            identity=tuple(channel['server'].get(k) for k in ('client_id','user_name','pass_word'))
            if identity not in allowed[b['businessType']]: raise Problem('共享通道凭据与配置不一致')

def generate(pid,plan_id,key):
    if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}",key): raise Problem("幂等键无效")
    with LOCK:
        r=read(DATA/"plans"/(valid_id(plan_id)+".json"))
        if r["projectId"]!=pid: raise Problem("计划不属于当前工程")
        for j in jobs():
            if j.get("projectId")==pid and j.get("key")==key:
                if j.get("planId")!=plan_id: raise Problem("幂等键已用于其他计划",409)
                return j
        p=get_project(pid)
        if r["revision"]!=p["revision"] or r["fingerprint"]!=digest(p): raise Problem("配置或映射已变化，请重新计算",409)
        if any(j["state"]=="running" for j in jobs()): raise Problem("已有任务执行中",409)
        j={"id":uid(),"kind":"gateway","projectName":p["name"],"projectId":pid,"planId":plan_id,"key":key,"created":now(),"state":"running","done":0,"total":0,"message":"正在生成并回读校验","platformVerified":False}
        note_job(j);threading.Thread(target=generate_worker,args=(j,p,r),daemon=True).start();return j
def generate_worker(j,p,r):
    folder=DATA/"packages"/j["id"];folder.mkdir(parents=True,exist_ok=True)
    partial=folder/"package.partial"
    try:
        payload=get_secret(r["planSecret"])
        plan,planned,resolved=engine(p).prepare(payload)
        if plan["planId"]!=r["enginePlanId"]: raise Problem("模板或引擎已变化，请重新计算",409)
        seen=set();client_ids=set();counters=Counter();j["total"]=len(plan["projects"]);note_job(j)
        credential_sets=shared_credentials(p) if p['gateway']['forwardMode']=='aggregate' else None
        summary_projects=[]
        with zipfile.ZipFile(partial,"w",zipfile.ZIP_DEFLATED) as z:
            for i,(project,expected) in enumerate(engine(p).iter_projects(plan,planned,resolved),1):
                entry=expected["name"]+"/tb_gateway.json"
                z.writestr(entry,json.dumps(project,ensure_ascii=False))
                written=json.loads(z.read(entry))
                engine(p).validate_project(written,expected,plan["snapshot"],client_ids,resolved,counters)
                verify_bindings(written,p,seen,credential_sets)
                summary_projects.append((expected['name'],expected,written))
                j["done"]=i;note_job(j)
            if seen!={b["collectAlias"] for b in active_bindings(p)}: raise Problem("本次选择的设备与生成工程不一致")
            from task_summary import generated_archive_summary
            z.writestr('summary.md',generated_archive_summary(
                p['name'],summary_projects,j,p['cloud']['base_url'].rstrip('/'),p.get('templateMode',False)))
            report={**r["public"],"filesValid":True,"platformVerified":False,"generatedAt":now(),"sourceRunId":p["sourceRunId"],"note":"工程文件已校验，凭据和平台上报未经在线验证"}
            z.writestr("validation.json",json.dumps(report,ensure_ascii=False,indent=2))
            from asset_templates import build_package
            import io
            with zipfile.ZipFile(io.BytesIO(build_package(p,lambda b:get_secret(b['credentialRef'])))) as templates:
                for name in templates.namelist():
                    z.writestr('asset-device-templates/'+name,templates.read(name))
        os.replace(partial,folder/"gateway-projects.zip")
        atomic(folder/"validation.json",report)
        j.update(state="completed",message="文件校验通过／上报未验证"+('；含模板凭据，需先导入平台 CSV 并核对凭据' if p.get('templateMode') else ''),summary=plan["summary"],filesValid=True)
    except Exception as e:
        partial.unlink(missing_ok=True);j.update(state="failed",message=safe_error(e),filesValid=False)
    j["finished"]=now();note_job(j);audit("generate",p["id"],j["state"])

def bind_template_pool(pid, data):
    """Resolve the complete template pool before atomically replacing its identities."""
    from app.platform.thingsboard_client import ThingsBoardClient
    from app.core.orchestrator import _extract_mqtt_credentials
    with LOCK:
        p = get_project(pid)
        if p['revision'] != data.get('revision'): raise Problem('配置已变化，请刷新',409)
        if not p.get('templateMode') or not p['bindings'] or p.get('testRound'):
            raise Problem('仅模板设备池可以重新绑定真实设备')
        if any(j['state']=='running' for j in jobs()): raise Problem('已有任务执行中',409)
        auth = data.get('auth',{})
        if any(not isinstance(auth.get(k),str) or not auth[k].strip() for k in ('existing_tenant_admin_username','existing_tenant_admin_password')):
            raise Problem('请填写已有租户管理员账号和密码')
        j = {'id':uid(),'kind':'cloud','projectId':pid,'projectName':p['name'],'created':now(),
             'state':'running','reuseExistingDevices':True,'message':'正在只读查询模板设备池的真实身份'}
        note_job(j)

    def worker():
        try:
            devices, relations, shared = [], [], []
            seen_ids, seen_clients = set(), set()
            url = p['cloud']['base_url'].rstrip('/')
            with ThingsBoardClient(url) as client:
                client.login(auth['existing_tenant_admin_username'],auth['existing_tenant_admin_password'])

                def resolve(binding):
                    name = binding['platformName']
                    ident = client.find_device_id_by_name(name)
                    if not ident: raise Problem('平台缺少设备：'+name+'；请完成 CSV 导入后重试，原模板绑定未更改')
                    raw = client.get_device_credentials(ident)
                    cid, user, secret, _ = _extract_mqtt_credentials(raw)
                    device = {'id':ident,'name':name,'type':binding.get('platformType','default'),
                              'notes':'template='+binding['businessType'].lower()+';',
                              'mqtt_client_id':cid,'mqtt_username':user,'mqtt_password_or_token':secret,
                              'raw':{'credentials':raw}}
                    credential = credentials_of(device,{})
                    if ident in seen_ids or credential['client_id'] in seen_clients:
                        raise Problem('平台设备 ID 或 MQTT 客户端 ID 重复；原模板绑定未更改')
                    seen_ids.add(ident);seen_clients.add(credential['client_id'])
                    return device, credential

                for binding in p['bindings']:
                    device, _ = resolve(binding)
                    devices.append(device)
                    for direction in ('from','to'):
                        for relation in client.get_device_relations(device['id'],direction):
                            source, target = relation.get('from',{}), relation.get('to',{})
                            if not all((source.get('id'),target.get('id'),source.get('entityType'),target.get('entityType'))):
                                raise Problem('平台关系缺少实体 ID 或类型；原模板绑定未更改')
                            relations.append({'from_id':source['id'],'to_id':target['id'],
                                              'from_entity_type':source['entityType'],'to_entity_type':target['entityType']})
                for item in shared_device_specs(p):
                    device, credential = resolve(item)
                    shared.append({**item,'platformId':device['id'],'platformUrl':url,
                                   'credentialRef':put_secret(credential),'credentialsPending':False,
                                   'credentialStatus':'imported_unverified'})
            bindings, relations, issues = build_bindings({'devices':devices,'relations':relations},p.get('profile','storage'))
            with LOCK:
                current = get_project(pid)
                if current['revision'] != p['revision']: raise Problem('查询期间配置已变化，请重新查询；原绑定未更改',409)
                current.update(bindings=bindings,relations=relations,mappingIssues=issues,sharedBindings=shared,
                               sourceStatus='SUCCESS',templateMode=False,sourceHash=digest(devices),updated=now())
                current.pop('templateMappingWarnings',None)
                current['revision'] += 1
                save_project(current)
            j.update(state='completed',message='真实设备身份已绑定，未修改平台资源；请重新生成工程包'+('；请确认 BMS 组合关系' if issues else '，可创建下一轮测试'))
        except Exception as e:
            j.update(state='failed',message=safe_error(e))
        j['finished']=now();note_job(j);audit('bind_template_pool',pid,j['state'])
    threading.Thread(target=worker,daemon=True).start()
    return j


def provision(pid,data):
    from app.models.config import RunConfig
    from app.core.orchestrator import QuickDataOrchestrator
    with LOCK:
        p=get_project(pid)
        reuse=data.get("reuseExistingDevices",False)
        if type(reuse) is not bool: raise Problem("复用已有设备参数无效")
        if p["revision"]!=data.get("revision"): raise Problem("配置已变化",409)
        if p["bindings"]: raise Problem("工程已有设备绑定，不可重复造数")
        if not p["pointsConfirmed"] or not sum(r["total"] for r in p["types"]): raise Problem("请先保存并确认设备与测点配置")
        if any(j["state"]=="running" for j in jobs()): raise Problem("已有任务执行中",409)
        if any(j.get("projectId")==pid and j.get("kind")=="cloud" and not (j.get("state")=="failed" and j.get("retrySafe") is True) for j in jobs()): raise Problem("本工程已提交过造数；请查看记录，防止重复创建。确需新任务请新建工程")
        try: engine(p).prepare(payload_for(p, require_binding=False))
        except ValueError as e: raise Problem(str(e))
        if not reuse:
            from app.services.templates import parse_template
            try: assets=parse_template(p["cloud"]["asset_template"])
            except ValueError as e: raise Problem(str(e))
            if p.get("profile")=="microgrid":
                count=p["microgrid"]["stationCount"]
                if not count or len(assets)!=1 or assets[0].count!=count: raise Problem("请填写并保存微电网电站数量")
            elif len(assets)!=3: raise Problem("资产模板需包含公司、电站、储能单元三行")
        for field in (("device_name_prefix",) if reuse else ("device_name_prefix","asset_name_prefix")):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}",str(p["cloud"].get(field,""))): raise Problem("新资源前缀须以字母开头，仅支持字母数字下划线短横线")
        auth=data.get("auth",{})
        required=("existing_tenant_admin_username","existing_tenant_admin_password")
        if any(not isinstance(auth.get(k),str) or not auth[k].strip() for k in required): raise Problem("请填写已有租户管理员账号和密码")
        conf={**p["cloud"],**{k:auth[k] for k in required},"mqtt_host":p["gateway"]["mqttHost"],"mqtt_port":p["gateway"]["mqttPort"],"mqtt_credentials_mode":"CLIENT_ID_USERNAME","enable_telemetry":False,"device_template":"\n".join(r["type"].lower()+","+str(r["total"])+",default" for r in p["types"] if r["total"]),"output_root":str(DATA/"cloud"),"enable_edge_creation":p["cloud"]["edge_mode"]=="create","enable_edge_assignment":p["cloud"]["edge_mode"]!="none"}
        conf.update(reuse_existing_devices=reuse, enable_device_creation=not reuse)
        if reuse:
            conf.update(enable_asset_creation=False, enable_relation_creation=False,
                        enable_rule_chain_creation=False, enable_edge_creation=False,
                        enable_edge_assignment=False, edge_mode="none", edge_names="", edge_count=0)
        relationships=[("A1","ASSET","A2","ASSET","Station"),("A2","ASSET","A3","ASSET","Container"),("A3","ASSET","pcs","DEVICE","PCS"),("A3","ASSET","bms","DEVICE","BMS"),("bms","DEVICE","groupbms","DEVICE","GroupBMS"),("groupbms","DEVICE","cell","DEVICE","Cell"),("A2","ASSET","cl","DEVICE","strategy"),("A2","ASSET","box","DEVICE","BOX")]
        if p.get("profile")=="microgrid":
            relationships=[("A1","ASSET",r["type"].lower(),"DEVICE",r["type"]) for r in p["types"] if r["total"]]
        conf["relation_rules"]="\n".join(",".join([a,at,b,bt,"auto","Contains",json.dumps({"type":t})]) for a,at,b,bt,t in relationships)
        try: config=RunConfig.model_validate(conf)
        except Exception: raise Problem("平台配置无效，请检查地址、边缘设置及设备模板")
        j={"id":uid(),"kind":"cloud","projectName":p["name"],"projectId":pid,"created":now(),"state":"running","message":"正在创建平台资源","done":0,"total":0}
        note_job(j)
        if reuse:
            j.update(reuseExistingDevices=True,message="正在只读查询已有设备、凭据和关联关系")
            note_job(j)
        def worker():
            try:
                result=QuickDataOrchestrator(config,run_id="run-"+j["id"]).run()
                if result.status!="SUCCESS" or any(not c.success for c in result.validation_checks):
                    from app.services.reporting import result_to_output_dict
                    j.update(cloud_failure(result_to_output_dict(result)))
                    raise Problem(j["message"])
                from app.services.reporting import result_to_output_dict
                doc=result_to_output_dict(result)
                bindings,relations,issues=build_bindings(doc,p.get("profile","storage"))
                with LOCK:
                    current=get_project(pid);current.update(bindings=bindings,relations=relations,mappingIssues=issues,sourceRunId=doc["runId"],sourceStatus="SUCCESS",sourceHash=digest({"devices":[b["platformId"] for b in bindings]}))
                    current["revision"]+=1;current["updated"]=now();save_project(current)
                if p['gateway']['forwardMode']=='aggregate':
                    prepare_shared_devices(get_project(pid),auth,create=not reuse)
                j.update(state="completed",message="已有设备和关系已复用，未修改平台资源；请计算网关工程" if reuse else "平台资源已创建，实际设备已回填；请计算网关工程")
            except Exception as e: j.update(state="failed",message=safe_error(e))
            j["finished"]=now();note_job(j);audit("provision",pid,j["state"])
        threading.Thread(target=worker,daemon=True).start();return j

def recover_jobs():
    for j in jobs():
        if j["state"]=="running":
            j.update(state="interrupted",message="服务重启，任务已中断；完成的历史工程不受影响");note_job(j)
        elif j.get("kind")=="cloud" and j["state"]=="failed" and "retrySafe" not in j:
            protected=DATA/"cloud"/("run-"+j["id"])/"entities.dpapi"
            try:
                result=json.loads(crypt(protected.read_bytes(),True))
                j.update(cloud_failure(result));note_job(j)
            except (OSError,ValueError):
                pass


def template_points(body):
    supplied=body.get('gateway',{})
    if not isinstance(supplied,dict): raise Problem('模板配置格式无效')
    config={}
    for key in ('templateDbPath','templateBmsCsvPath'):
        value=supplied.get(key,defaults()[key])
        if not isinstance(value,str) or not value.strip() or any(ord(c)<32 for c in value):
            raise Problem('请填写有效的模板完整路径')
        config[key]=value.strip()
    templates={}
    sources=[]
    for key,extension in (('templateDbPath','.xml'),('templateBmsCsvPath','.csv')):
        path=Path(config[key])
        if path.suffix.lower()!=extension or not path.is_file(): raise Problem('请选择存在的 '+extension+' 模板文件')
        try:
            blob=path.read_bytes()
            parsed=g.parse_template(blob,path.name)
        except (OSError,ValueError,UnicodeError): raise Problem('模板无法读取或格式不正确：'+path.name)
        sources.append({'name':path.name,'sha256':hashlib.sha256(blob).hexdigest()})
        templates.update({k.upper():v.get('tags',[]) for k,v in parsed.get('templates',{}).items()})
    templates.update({k.upper():v for k,v in g.bms_segments(templates.get('BMS',[])).items()})
    kind=body.get('type','PCS')
    if kind not in KINDS: raise Problem('模板设备类型无效')
    search=str(body.get('search','')).lower()
    tags=[g.normalize_tag(t,i,kind) for i,t in enumerate(templates.get(kind,[]),1)]
    tags=[t for t in tags if search in (t['n']+' '+t['d']).lower()]
    page=w.integer(body.get('page',0),'页码',0,1000000)
    page=min(page,max(0,(len(tags)-1)//100))
    return {'sources':sources,'counts':{k:len(templates.get(k,[])) for k in KINDS},'page':page,'total':len(tags),
            'points':[{'tag':t['n'],'description':t['d'],'interval':t['p'],'generator':t['s0']} for t in tags[page*100:page*100+100]]}


def shared_device_specs(p):
    if p['gateway']['forwardMode']!='aggregate': return []
    payload=payload_for(p,require_binding=False)
    config,rows=engine(p).normalize(payload) if p.get('profile')!='microgrid' else (payload['config'],payload['types'])
    planned,_,_=w.allocate(config,rows)
    prefix=p['gateway'].get('sharedDevicePrefix') or p['cloud'].get('device_name_prefix','')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}',prefix): raise Problem('请填写平台设备名前缀或共享通道设备前缀')
    result=[]
    for row in planned:
        size=max(1,config['maxForwardTags']//row['points'])
        count=sum((n+size-1)//size for n in row['projects'])
        result.extend({'businessType':row['type'],'collectAlias':row['prefix']+str(i),
                       'platformName':prefix+row['prefix']+'z'+str(i)} for i in range(1,count+1))
    return result

def shared_device_plan(p):
    specs=shared_device_specs(p)
    pool={b['platformName']:b for b in p.get('sharedBindings',[]) if b.get('platformUrl')==p['cloud']['base_url'].rstrip('/')}
    for item in specs:
        b=pool.get(item['platformName'],{})
        item['ready']=bool(b.get('credentialRef')) and not b.get('credentialsPending',False)
    return {'total':len(specs),'missing':sum(not x['ready'] for x in specs),'devices':specs}

def prepare_shared_devices(p,auth,create=False):
    from app.platform.thingsboard_client import ThingsBoardClient,extract_entity_id
    specs=shared_device_specs(p)
    if not specs: return
    url=p['cloud']['base_url'].rstrip('/')
    with ThingsBoardClient(url) as client:
        client.login(auth['existing_tenant_admin_username'],auth['existing_tenant_admin_password'])
        for item in specs:
            current=get_project(p['id'])
            saved=next((b for b in current.get('sharedBindings',[]) if b['platformName']==item['platformName'] and b.get('platformUrl')==url),{})
            device_id=client.find_device_id_by_name(item['platformName'])
            owned_pending=saved.get('credentialsPending') and saved.get('platformId')==device_id
            if not device_id:
                if not create: raise Problem('平台缺少共享通道设备：'+item['platformName']+'；请使用补建共享通道设备')
                device_id=extract_entity_id(client.create_device(item['platformName'],'default'))
                if not device_id: raise Problem('共享通道设备创建结果缺少 ID')
                owned_pending=True
                pending={**item,'platformId':device_id,'platformUrl':url,'credentialsPending':True}
                with LOCK:
                    current=get_project(p['id'])
                    current['sharedBindings']=[b for b in current.get('sharedBindings',[]) if not (b['platformName']==item['platformName'] and b.get('platformUrl')==url)]+[pending]
                    current['revision']+=1;current['updated']=now();save_project(current)
            if owned_pending:
                if not create: raise Problem('共享通道设备凭据尚未完成，请使用补建继续处理：'+item['platformName'])
                client.ensure_device_mqtt_credentials(device_id,item['platformName'],'CLIENT_ID_USERNAME')
            raw=client.get_device_credentials(device_id)
            typ=raw.get('credentialsType')
            if typ=='MQTT_BASIC':
                value=raw.get('credentialsValue') or '{}'
                values=json.loads(value) if isinstance(value,str) else value
                d={'id':device_id,'mqtt_client_id':values.get('clientId',''),'mqtt_username':values.get('userName',''),'mqtt_password_or_token':values.get('password',''),'raw':{'credentials':raw}}
            elif typ=='ACCESS_TOKEN':
                d={'id':device_id,'mqtt_password_or_token':raw.get('credentialsId',''),'raw':{'credentials':raw}}
            else: raise Problem('共享通道设备凭据类型不支持：'+item['platformName'])
            binding={**item,'platformId':device_id,'platformUrl':url,'credentialRef':put_secret(credentials_of(d,{})),'credentialsPending':False}
            with LOCK:
                current=get_project(p['id'])
                current['sharedBindings']=[b for b in current.get('sharedBindings',[]) if not (b['platformName']==item['platformName'] and b.get('platformUrl')==url)]+[binding]
                current['revision']+=1;current['updated']=now();save_project(current)

def provision_shared(pid,body):
    with LOCK:
        p=get_project(pid)
        if body.get('revision')!=p['revision']: raise Problem('配置已变化，请刷新',409)
        if any(j['state']=='running' for j in jobs()): raise Problem('已有任务执行中',409)
        if not p['bindings'] or p['sourceStatus']!='SUCCESS': raise Problem('请先完成业务设备准备')
        auth=body.get('auth',{})
        if any(not isinstance(auth.get(k),str) or not auth[k].strip() for k in ('existing_tenant_admin_username','existing_tenant_admin_password')): raise Problem('请填写租户管理员账号和密码')
        create=body.get('createMissing',False)
        if type(create) is not bool: raise Problem('补建参数无效')
        specs=shared_device_specs(p)
        if not specs: raise Problem('当前无需准备共享通道设备')
        j={'id':uid(),'kind':'shared','projectId':pid,'projectName':p['name'],'state':'running','created':now(),'total':len(specs),'done':0,'message':'正在准备共享通道设备'}
        note_job(j)
        def worker():
            try:
                prepare_shared_devices(p,auth,create)
                j.update(state='completed',done=len(specs),message='共享通道设备及实际 MQTT 凭据已绑定')
            except Exception as exc:
                message=str(exc) if isinstance(exc,Problem) else '请检查平台连接及账号权限'
                j.update(state='failed',message='共享通道设备准备未完成：'+message+'；已保存的通道设备保留，可重试继续')
            j['finished']=now();note_job(j);audit('shared_devices',pid,j['state'])
        threading.Thread(target=worker,daemon=True).start()
        return j
