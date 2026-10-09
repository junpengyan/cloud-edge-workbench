import copy, json, os, shutil, tempfile, time, unittest, zipfile
from pathlib import Path
TEST_DATA=tempfile.TemporaryDirectory(prefix="unified-tests-")
os.environ["UNIFIED_DATA_DIR"]=TEST_DATA.name
import workspace as s

def fixture():
    ds=[]
    for i,k in enumerate(s.KINDS):
        ds.append({"id":"platform-"+k,"name":"fixture-"+k.lower()+"7","type":"default","notes":"template="+k.lower()+"; templateIndex="+str(i+1),"mqtt_client_id":"test-client-"+k,"mqtt_username":"test-user-"+k,"mqtt_password_or_token":"fake-test-secret-"+k,"raw":{"credentials":{"credentialsType":"MQTT_BASIC"}}})
    return {"runId":"run-test","status":"SUCCESS","devices":ds,"relations":[{"from_entity_type":"DEVICE","to_entity_type":"DEVICE","from_id":"platform-BMS","to_id":"platform-GROUPBMS"},{"from_entity_type":"DEVICE","to_entity_type":"DEVICE","from_id":"platform-GROUPBMS","to_id":"platform-CELL"}],"configSnapshot":{"mqtt_host":"127.0.0.1","mqtt_port":1883}}
def ready():
    p=s.import_doc(fixture(),"run-fixture","fixturehash")
    data=copy.deepcopy(p)
    for r in data["types"]: r["points"]=130 if r["type"]=="CL" else s.CANDIDATE[r["type"]]
    data["pointsConfirmed"]=True
    return s.update(p["id"],data)
def wait_job(j):
    for _ in range(300):
        j=s.read(s.job_path(j["id"]))
        if j["state"]!="running": return j
        time.sleep(.02)
    raise AssertionError("job timed out")

class WorkspaceTests(unittest.TestCase):
    def test_project_delete_restore_preserves_bindings_and_history(self):
        p=ready();old=copy.deepcopy(p)
        j={"id":s.uid(),"projectId":p['id'],"state":"completed","created":s.now(),"kind":"gateway"};s.note_job(j)
        deleted=s.set_project_deleted(p['id'],p['revision'],True)
        self.assertNotIn(p['id'],[x['id'] for x in s.projects()])
        self.assertIn(p['id'],[x['id'] for x in s.projects(True)])
        with self.assertRaises(s.Problem):s.get_project(p['id'])
        with self.assertRaises(s.Problem):s.prepare(p['id'],deleted['revision'])
        self.assertEqual(s.read(s.job_path(j['id'])),j)
        s.set_project_deleted(p['id'],deleted['revision'],False)
        self.assertEqual(s.get_project(p['id'])['bindings'],old['bindings'])
        self.assertEqual(s.get_project(p['id'])['types'],old['types'])
        self.assertTrue((s.DATA/'projects'/p['id']/('v%d.json'%p['revision'])).exists())

    def test_permanent_delete_removes_only_trashed_local_project_artifacts(self):
        p=s.new_project('永久删除样例');s.save_project(p)
        secret=s.uid();(s.DATA/'vault'/(secret+'.bin')).write_bytes(b'fixture')
        plan={'id':s.uid(),'projectId':p['id'],'planSecret':secret}
        s.atomic(s.DATA/'plans'/(plan['id']+'.json'),plan)
        job={'id':s.uid(),'projectId':p['id'],'state':'completed','kind':'gateway','created':s.now()}
        s.note_job(job)
        package=s.DATA/'packages'/job['id'];package.mkdir(parents=True)
        (package/'gateway-projects.zip').write_bytes(b'fixture')
        deployment={'id':s.uid(),'projectId':p['id'],'state':'completed'}
        deploy_folder=s.DATA/'deployments'/deployment['id'];deploy_folder.mkdir(parents=True)
        s.atomic(deploy_folder/'record.json',deployment)
        deleted=s.set_project_deleted(p['id'],p['revision'],True)
        with self.assertRaises(s.Problem):
            s.permanently_delete_project(p['id'],deleted['revision'],'wrong name')
        result=s.permanently_delete_project(p['id'],deleted['revision'],p['name'])
        self.assertEqual(result,{'id':p['id'],'deleted':True})
        self.assertFalse(s.project_path(p['id']).exists())
        self.assertFalse((s.DATA/'projects'/p['id']).exists())
        self.assertFalse(s.job_path(job['id']).exists())
        self.assertFalse(package.exists())
        self.assertFalse((s.DATA/'plans'/(plan['id']+'.json')).exists())
        self.assertFalse((s.DATA/'vault'/(secret+'.bin')).exists())
        self.assertFalse(deploy_folder.exists())

    def test_permanent_delete_rejects_live_project_and_dependent_round(self):
        p=s.new_project('来源池');s.save_project(p)
        with self.assertRaises(s.Problem):s.permanently_delete_project(p['id'],p['revision'],p['name'])
        child=s.new_project('关联轮次');child['testRound']={'poolId':p['id'],'poolRevision':p['revision'],'poolName':p['name'],'poolDevices':0};s.save_project(child)
        deleted=s.set_project_deleted(p['id'],p['revision'],True)
        with self.assertRaises(s.Problem):s.permanently_delete_project(p['id'],deleted['revision'],p['name'])
        self.assertTrue(s.project_path(p['id']).exists())

    def test_gateway_zip_download_uses_project_name(self):
        from fastapi.testclient import TestClient
        from urllib.parse import quote
        import server
        job={'id':s.uid(),'projectId':s.uid(),'projectName':'中文工程','kind':'gateway',
             'state':'completed','filesValid':True,'created':s.now()}
        s.note_job(job)
        package=s.DATA/'packages'/job['id'];package.mkdir(parents=True)
        (package/'gateway-projects.zip').write_bytes(b'fixture')
        response=TestClient(server.app).get('/api/jobs/'+job['id']+'/download')
        self.assertEqual(response.status_code,200)
        self.assertIn(quote('中文工程.zip'),response.headers['content-disposition'])

    def test_source_manual_estimate_uses_protected_local_run(self):
        p=s.new_project('来源估算')
        p['sourceRunId']='run-'+s.uid()
        folder=s.DATA/'cloud'/p['sourceRunId'];folder.mkdir(parents=True)
        estimate={'automation_minutes':18.79,'manual_minutes':35404.0,'saved_minutes':35385.21,'saved_ratio':0.9995}
        (folder/'entities.dpapi').write_bytes(s.crypt(json.dumps({'manualEstimate':estimate}).encode()))
        self.assertEqual(s.source_manual_estimate(p),estimate)

    def test_permanent_delete_keeps_credentials_used_by_other_project(self):
        secret=s.uid();path=s.DATA/'vault'/(secret+'.bin');path.write_bytes(b'shared')
        source=s.new_project('待删除');source['bindings']=[{'credentialRef':secret}];s.save_project(source)
        other=s.new_project('保留');other['bindings']=[{'credentialRef':secret}];s.save_project(other)
        deleted=s.set_project_deleted(source['id'],source['revision'],True)
        s.permanently_delete_project(source['id'],deleted['revision'],source['name'])
        self.assertTrue(path.exists())
        self.assertTrue(s.project_path(other['id']).exists())

    def test_delete_rejects_active_task_and_stale_version(self):
        p=ready()
        with self.assertRaises(s.Problem):s.set_project_deleted(p['id'],p['revision']-1,True)
        j={"id":s.uid(),"projectId":p['id'],"state":"running","created":s.now(),"kind":"gateway"};s.note_job(j)
        try:
            with self.assertRaisesRegex(s.Problem,'正在执行'):s.set_project_deleted(p['id'],p['revision'],True)
            self.assertEqual(s.get_project(p['id']),p)
        finally:
            j['state']='completed';s.note_job(j)
    def test_existing_tenant_execution_never_logs_in_sysadmin(self):
        from unittest.mock import patch, MagicMock
        from app.models.config import RunConfig
        from app.models.entities import RunResult
        from app.core.orchestrator import QuickDataOrchestrator
        cfg=RunConfig(base_url='http://127.0.0.1:9999',existing_tenant_admin_username='tenant-fixture',existing_tenant_admin_password='fixture-password',enable_edge_creation=False,enable_edge_assignment=False,enable_rule_chain_creation=False,enable_telemetry=False)
        runner=QuickDataOrchestrator(cfg)
        client=MagicMock()
        with patch('app.core.orchestrator.ThingsBoardClient',return_value=client) as factory, patch.object(runner,'_precheck'), patch.object(runner,'_create_assets') as assets, patch.object(runner,'_create_devices') as devices:
            runner._run_steps(RunResult(run_id='fixture'))
        factory.assert_called_once()
        client.login.assert_called_once_with('tenant-fixture','fixture-password')
        client.get_password_policy.assert_not_called()
        client.create_tenant.assert_not_called()
        self.assertIs(assets.call_args.args[0],client)
        self.assertIs(devices.call_args.args[0],client)
        client.close.assert_called_once()

    def test_precheck_failure_allows_only_proven_safe_retry(self):
        doc={"status":"FAILED","error":"云平台连接预检失败。 [WinError 10061]","configSnapshot":{"base_url":"http://test.invalid"}}
        result=s.cloud_failure(doc)
        self.assertTrue(result["retrySafe"])
        self.assertIn("test.invalid:80",result["message"])
        self.assertFalse(s.cloud_failure({**doc,"devices":[{"id":"partial"}]})["retrySafe"])
        self.assertFalse(s.cloud_failure({**doc,"error":"POST failed timeout"})["retrySafe"])

    def test_recover_old_precheck_failure_without_deleting_history(self):
        jid=s.uid();j={"id":jid,"kind":"cloud","state":"failed","created":s.now(),"message":"old"}
        s.note_job(j)
        f=s.DATA/"cloud"/("run-"+jid)/"entities.dpapi";f.parent.mkdir(parents=True)
        f.write_bytes(s.crypt(json.dumps({"status":"FAILED","error":"云平台连接预检失败。"}).encode()))
        s.recover_jobs()
        self.assertTrue(s.read(s.job_path(jid))["retrySafe"])
        self.assertTrue(f.exists())

    def test_cloud_report_encrypted_without_plaintext_exports(self):
        from app.models.entities import RunResult, EntityRecord
        from secure_reporting import write_reports
        result=RunResult(run_id="run-encryption-test",status="FAILED",error="fake-sensitive-error",
            devices=[EntityRecord(entity_type="DEVICE",name="synthetic-device",mqtt_password_or_token="fake-report-secret")])
        target=s.DATA/"report-encryption-test"
        write_reports(result,target)
        self.assertEqual({p.name for p in target.iterdir()},{"entities.dpapi","summary.json"})
        protected=(target/"entities.dpapi").read_bytes()
        self.assertNotIn(b"fake-report-secret",protected)
        decoded=json.loads(s.crypt(protected,True))
        self.assertEqual(decoded["devices"][0]["mqtt_password_or_token"],"fake-report-secret")
        summary=(target/"summary.json").read_text(encoding="utf-8")
        self.assertNotIn("fake-sensitive-error",summary)
        self.assertNotIn("fake-report-secret",summary)
        self.assertFalse(json.loads(summary)["platformVerified"])

    def test_import_and_dpapi_no_plaintext(self):
        p=s.import_doc(fixture(),"run-fixture","x")
        self.assertFalse(p["pointsConfirmed"])
        self.assertEqual(len(p["bindings"]),6)
        self.assertFalse(p["mappingIssues"])
        text=json.dumps(p)
        self.assertNotIn("fake-test-secret",text)
        for b in p["bindings"]:
            self.assertNotIn(b"fake-test-secret",(s.DATA/"vault"/(b["credentialRef"]+".bin")).read_bytes())
            self.assertEqual(s.get_secret(b["credentialRef"])["client_id"],"test-client-"+b["businessType"])
    def test_duplicate_ids_rejected(self):
        d=fixture();d["devices"][1]["id"]=d["devices"][0]["id"]
        with self.assertRaises(s.Problem): s.import_doc(d,"run-x","x")
    def test_missing_points_and_aggregate_blocked(self):
        p=s.import_doc(fixture(),"run-fixture","x")
        with self.assertRaisesRegex(s.Problem,"确认"): s.prepare(p["id"],p["revision"])
        p=ready();p["gateway"]["forwardMode"]="aggregate";s.save_project(p)
        with self.assertRaisesRegex(s.Problem,"共享"): s.prepare(p["id"],p["revision"])
    def test_relation_conflict_requires_explicit_stacks(self):
        d=fixture();d["relations"]=[]
        p=s.import_doc(d,"run-test","x")
        self.assertTrue(p["mappingIssues"])
        m={b["businessType"]:b["logicalId"] for b in p["bindings"] if b["stackIndex"]}
        p=s.set_stacks(p["id"],p["revision"],[m])
        self.assertEqual(p["mappingIssues"],[])
        with self.assertRaises(s.Problem): s.set_stacks(p["id"],p["revision"],[m,m])
    def test_stale_plan_and_revision(self):
        p=ready();plan=s.prepare(p["id"],p["revision"])
        s.update(p["id"],copy.deepcopy(p))
        with self.assertRaises(s.Problem): s.generate(p["id"],plan["planId"],"stale-test-key")
        with self.assertRaises(s.Problem): s.update(p["id"],p)
    def test_generate_exact_identity_and_segments(self):
        p=ready();plan=s.prepare(p["id"],p["revision"])
        self.assertEqual(plan["summary"]["totalDevices"],6)
        self.assertEqual(plan["summary"]["collectChannels"],4)
        self.assertEqual(plan["summary"]["forwardChannels"],6)
        self.assertNotIn("fake-test-secret",json.dumps(plan))
        j=s.generate(p["id"],plan["planId"],"idem-test-key")
        same=s.generate(p["id"],plan["planId"],"idem-test-key")
        self.assertEqual(j["id"],same["id"])
        j=wait_job(j);self.assertEqual(j["state"],"completed",j["message"])
        archive=s.DATA/"packages"/j["id"]/"gateway-projects.zip"
        with zipfile.ZipFile(archive) as z:
            docs=[json.loads(z.read(n)) for n in z.namelist() if n.endswith("tb_gateway.json")]
            report=json.loads(z.read("validation.json"));self.assertFalse(report["platformVerified"]);self.assertTrue(report["filesValid"])
            self.assertEqual([name for name in z.namelist() if name.endswith('summary.md')],['summary.md'])
            summary=z.read('summary.md').decode('utf-8')
            self.assertNotIn('人力节省估算',summary)
            self.assertNotIn('预计节省比例：',summary)
        aliases={}
        for doc in docs:
            for c in doc["corridors_zf"]:
                alias=c["alias_name"];aliases[alias]=c
                kind=alias[:-1].upper()
                self.assertEqual(c["server"]["client_id"],"test-client-"+kind)
                self.assertEqual(c["server"]["pass_word"],"fake-test-secret-"+kind)
        for alias,first,last in (("bms1",1,290),("groupbms1",291,390),("cell1",391,1290)):
            points=aliases[alias]["server"]["devices"][0]["point_table"]
            self.assertEqual(len(points),last-first+1)
            self.assertEqual(int(points[0]["point_key_in"].split("Tag")[-1]),first)
            self.assertEqual(int(points[-1]["point_key_in"].split("Tag")[-1]),last)
            self.assertEqual(len({x["point_key_in"] for x in points}),len(points))
        bad=copy.deepcopy(docs[0]);bad["corridors_zf"][0]["server"]["pass_word"]="wrong"
        with self.assertRaises(s.Problem):s.verify_bindings(bad,p,set())
    def test_blank_username_client_id_mode(self):
        d=fixture()
        for v in d["devices"]:v["mqtt_username"]="";v["mqtt_password_or_token"]=""
        p=s.import_doc(d,"run-client-only","x")
        data=copy.deepcopy(p)
        for r in data["types"]:r["points"]=130 if r["type"]=="CL" else s.CANDIDATE[r["type"]]
        data["pointsConfirmed"]=True;p=s.update(p["id"],data)
        self.assertEqual(s.prepare(p["id"],p["revision"])["summary"]["totalDevices"],6)
    def test_path_and_api_boundaries(self):
        from fastapi.testclient import TestClient
        from server import app,TOKEN
        with self.assertRaises(s.Problem): s.history_path("../bad")
        with TestClient(app) as c:
            self.assertEqual(c.get("/api/health").status_code,200)
            self.assertEqual(c.post("/api/projects",json={}).status_code,403)
            self.assertEqual(c.post("/api/projects",headers={"X-Workspace-Token":TOKEN,"Origin":"http://evil.example"},json={}).status_code,403)
            self.assertEqual(c.get("/data/vault/test.bin").status_code,404)
            self.assertEqual(c.post("/api/import",headers={"X-Workspace-Token":TOKEN},json={"runId":"../x"}).status_code,422)
    def test_zero_types(self):
        d=fixture();d["devices"]=d["devices"][:1];d["relations"]=[]
        p=s.import_doc(d,"run-pcs-only","x");data=copy.deepcopy(p)
        data["types"][0]["points"]=290;data["pointsConfirmed"]=True;p=s.update(p["id"],data)
        self.assertEqual(s.prepare(p["id"],p["revision"])["summary"]["totalDevices"],1)


    def test_new_cloud_preflight_and_zero_types_without_network(self):
        from unittest.mock import patch
        from app.models.entities import RunResult, EntityRecord
        p=s.new_project("离线新建测试")
        p["types"][0]["total"]=1
        for row in p["types"]:row["points"]=s.CANDIDATE[row["type"]]
        p["pointsConfirmed"]=True;p["gateway"]["mqttHost"]="127.0.0.1";p["cloud"]["base_url"]="http://example.invalid"
        s.save_project(p)
        f=fixture();f["devices"]=f["devices"][:1]
        result=RunResult(run_id="run-mock",status="SUCCESS",devices=[EntityRecord(**v,entity_type="DEVICE") for v in f["devices"]])
        with patch("app.core.orchestrator.QuickDataOrchestrator") as runner:
            runner.return_value.run.return_value=result
            j=s.provision(p["id"],{"revision":1,"auth":{k:"synthetic-only" for k in ("sysadmin_username","sysadmin_password","existing_tenant_admin_username","existing_tenant_admin_password")}})
            j=wait_job(j)
            self.assertEqual(j["state"],"completed",j["message"])
            cfg=runner.call_args.args[0]
            self.assertEqual(cfg.device_template,"pcs,1,default")
            self.assertIn("pcs,DEVICE",cfg.relation_rules)
            self.assertFalse(cfg.enable_telemetry)
        self.assertEqual(len(s.get_project(p["id"])["bindings"]),1)
        with self.assertRaises(s.Problem):s.provision(p["id"],{"revision":2,"auth":{}})
    def test_recovery_marks_interrupted(self):
        j={"id":s.uid(),"state":"running","kind":"gateway","projectId":s.uid(),"created":s.now()}
        s.note_job(j);s.recover_jobs()
        self.assertEqual(s.read(s.job_path(j["id"]))["state"],"interrupted")

if __name__=="__main__":unittest.main()
