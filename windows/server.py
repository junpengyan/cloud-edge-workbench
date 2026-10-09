from __future__ import annotations
import json, os, re, secrets
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlsplit
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
import workspace as s
import deployment as d
import capacity

TOKEN=secrets.token_urlsafe(32)
app=FastAPI(title="物联网测试工程自动化平台",docs_url=None,redoc_url=None,openapi_url=None)

def archive_filename(name):
    clean=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name or '')).strip(' .')[:100]
    return (clean or '网关工程')+'.zip'
app.mount("/static",StaticFiles(directory=s.BASE/"static"),name="static")
s.recover_jobs()

@app.middleware("http")
async def boundary(request:Request,call_next):
    host=(request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1","localhost","testserver"):
        return JSONResponse({"message":"仅允许本机访问"},403)
    if request.method not in ("GET","HEAD","OPTIONS"):
        if request.headers.get("x-workspace-token")!=TOKEN:
            return JSONResponse({"message":"会话已失效，请刷新页面"},403)
        origin=request.headers.get("origin")
        if origin and urlsplit(origin).netloc!=request.headers.get("host"):
            return JSONResponse({"message":"请求来源无效"},403)
        length=request.headers.get("content-length","0")
        if not length.isdigit() or int(length)>4000000:
            return JSONResponse({"message":"请求过大"},413)
    response=await call_next(request)
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response

@app.exception_handler(s.Problem)
async def problem(request,exc): return JSONResponse({"message":str(exc)},exc.status)
@app.exception_handler(ValueError)
async def invalid(request,exc): return JSONResponse({"message":"输入或模板配置无效"},422)

@app.get("/")
def index(): return FileResponse(s.BASE/"static"/"index.html")
@app.get("/api/health")
def health(): return {"app":"unified-phase-one","version":"1.2.0","port":int(os.getenv("UNIFIED_PORT","18768"))}
@app.get("/api/bootstrap")
def bootstrap(): return {"token":TOKEN,"history":s.history_list(),"projects":s.projects(),"jobs":s.jobs(),"candidate":s.CANDIDATE,"defaults":s.defaults()}
@app.get("/api/projects")
def projects(): return s.projects()
@app.get("/api/deleted-projects")
def deleted_projects(): return s.projects(deleted=True)
@app.post("/api/projects/{pid}/delete")
def delete_project(pid:str,body:dict): return s.set_project_deleted(pid,body.get("revision"),True)
@app.post("/api/projects/{pid}/restore")
def restore_project(pid:str,body:dict): return s.set_project_deleted(pid,body.get("revision"),False)
@app.post("/api/projects/{pid}/purge")
def purge_project(pid:str,body:dict):
    return s.permanently_delete_project(pid,body.get('revision'),body.get('confirmName'))
@app.get("/api/projects/{pid}")
def get_project(pid:str): return s.get_project(pid)
@app.post("/api/projects")
def create(body:dict):
    p=s.new_project(body.get("name","新工程"),body.get("profile","storage"));s.save_project(p);return p
@app.post("/api/import")
def import_run(body:dict):
    with s.LOCK: return s.import_run(body.get("runId"),body.get("profile","storage"))
@app.post("/api/projects/{pid}/save")
def save(pid:str,body:dict): return s.update(pid,body)
@app.post('/api/projects/{pid}/capacity')
def capacity_plan(pid:str,body:dict):
    p=s.get_project(pid)
    if p['revision']!=body.get('revision'): raise s.Problem('工程版本已变化，请刷新',409)
    if p.get('profile','storage') == 'storage' and 'capacityMwh' in body:
        limits=Counter(b['businessType'] for b in p['bindings']) if p['bindings'] and not p.get('templateMode') else None
        result = capacity.storage_capacity(body.get('types'), body.get('capacityMwh'),limits)
    else:
        result = capacity.calculate(p.get('profile','storage'),body.get('types'),body.get('target'),body.get('mode'))
    if p['bindings'] and not p.get('templateMode'):
        s.active_bindings({**p, 'types':[{**r, 'total':result['totals'][r['type']]} for r in p['types']]})
    return result
@app.post("/api/projects/{pid}/stacks")
def stacks(pid:str,body:dict):
    return s.set_stacks(pid,body.get("revision"),body.get("mapping",[]))
@app.post('/api/projects/{pid}/configuration-check')
def configuration_check(pid:str,body:dict):
    with s.LOCK:
        p=s.get_project(pid)
        if p['revision']!=body.get('revision'): raise s.Problem('配置已变化，请保存后重新检查',409)
        try:
            # Device relation repair belongs to step 3; step 2 checks configuration only.
            configuration = {**p, 'mappingIssues':[]}
            plan, _, _ = s.engine(p).prepare(s.payload_for(configuration, require_binding=False))
        except ValueError as exc:
            raise s.Problem(str(exc))
        return {'revision':p['revision'], 'summary':plan['summary'], 'templates':plan['templates'], 'sharedDevices':s.shared_device_specs(p)}

@app.post("/api/projects/{pid}/plan")
def plan(pid:str,body:dict): return s.prepare(pid,body.get("revision"))
@app.post('/api/projects/{pid}/continue-templates')
def continue_templates(pid:str,body:dict): return s.continue_with_templates(pid,body.get('revision'))
@app.post("/api/projects/{pid}/generate")
def generate(pid:str,body:dict): return s.generate(pid,body.get("planId"),body.get("key"))
@app.post("/api/projects/{pid}/provision")
def provision(pid:str,body:dict): return s.provision(pid,body)

@app.post("/api/projects/{pid}/bind-template-pool")
def bind_template_pool(pid:str,body:dict): return s.bind_template_pool(pid,body)
@app.get("/api/jobs")
def jobs(): return s.jobs()
@app.get("/api/jobs/{jid}")
def job(jid:str): return s.read(s.job_path(jid))
@app.get("/api/jobs/{jid}/download")
def download(jid:str):
    j=s.read(s.job_path(jid))
    if j["kind"]!="gateway" or j["state"]!="completed" or not j.get("filesValid"): raise s.Problem("工程未完成校验，不能下载",409)
    p=s.DATA/"packages"/s.valid_id(jid)/"gateway-projects.zip"
    if not p.is_file(): raise s.Problem("工程文件缺失",404)
    return FileResponse(p,filename=archive_filename(j.get('projectName')))
@app.get("/api/jobs/{jid}/report")
def report(jid:str):
    j=s.read(s.job_path(jid))
    if j["kind"]!="gateway" or j["state"]!="completed": raise s.Problem("验收报告尚未生成",409)
    return s.read(s.DATA/"packages"/s.valid_id(jid)/"validation.json")

@app.get('/deployment')
def deployment_page(): return FileResponse(s.BASE/'static'/'deployment.html')

@app.get('/api/deployment-sources')
def deployment_sources(): return d.generated_sources()

@app.post('/api/jobs/{jid}/deployment')
def generated_deployment_prepare(jid:str,body:dict): return d.prepare_generated(jid,body)

@app.get('/api/deployments')
def deployments(): return d.records()

@app.post('/api/projects/{pid}/deployment')
def deployment_prepare(pid:str,body:dict): return d.prepare(pid,body)

@app.get('/api/deployments/{jid}')
def deployment_record(jid:str): return d.record(jid)

def deployment_call(fn,*args):
    try: return fn(*args)
    except s.Problem: raise
    except Exception: raise s.Problem('连接或认证失败，请核对网关地址和 SSH 登录信息；未执行自动重试',502)

@app.post('/api/deployments/{jid}/check')
def deployment_check(jid:str,body:dict): return deployment_call(d.check,jid,body)

@app.post('/api/deployments/{jid}/start')
def deployment_start(jid:str,body:dict): return deployment_call(d.start,jid,body)

@app.post('/api/deployments/{jid}/resume')
def deployment_resume(jid:str): return deployment_call(d.resume,jid)

@app.post('/api/deployments/{jid}/stop')
def deployment_stop(jid:str): return deployment_call(d.stop,jid)

@app.get('/api/deployments/{jid}/download')
def deployment_download(jid:str):
    filename=archive_filename(d.record(jid).get('sourceName'))
    return Response(d.package(jid),media_type='application/zip',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(filename)})

@app.get('/api/deployments/{jid}/report')
def deployment_report(jid:str):
    return JSONResponse(d.record(jid),headers={'Content-Disposition':'attachment; filename="validation-report-'+s.valid_id(jid)[:8]+'.json"'})

@app.post('/api/template-points')
def template_points(body:dict): return s.template_points(body)

@app.post('/api/projects/{pid}/shared-devices')
def shared_devices(pid:str,body:dict): return s.provision_shared(pid,body)

@app.post('/api/projects/{pid}/refresh-pool')
def refresh_pool(pid:str,body:dict):
    import rounds
    return rounds.refresh_pool(pid,body)

@app.get('/api/projects/{pid}/round-pool')
def round_pool(pid:str, mode:str="snapshot"):
    import rounds
    return rounds.info(pid, mode)

@app.post('/api/projects/{pid}/round-preview')
def round_preview(pid:str,body:dict):
    import rounds
    return rounds.preview(pid,body)

@app.post('/api/projects/{pid}/rounds')
def create_round(pid:str,body:dict):
    import rounds
    return rounds.create(pid,body)


@app.get('/api/overview')
def overview():
    import rounds
    return rounds.overview()

@app.post('/api/deployments/{jid}/assessment')
def deployment_assessment(jid:str,body:dict): return d.assessment(jid,body)







@app.post('/api/projects/{pid}/round-allocation')
def round_allocation(pid:str, body:dict):
    import rounds
    return rounds.allocate(pid, body)


@app.post('/api/deployments/{jid}/telemetry-check')
def telemetry_check(jid:str, body:dict):
    import acceptance
    return acceptance.start_check(jid, body)

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=int(os.getenv("UNIFIED_PORT","18768")),access_log=False)
