import copy
import io
import json
import unittest
import zipfile
import tempfile
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_microgrid import micro
from test_workspace import s
import deployment as d

class DeploymentTests(unittest.TestCase):
    def test_deployment_requires_only_ssh_credentials(self):
        self.assertEqual(d.auth({'sshUsername':'fixture', 'sshPassword':'synthetic'}),
                         {'sshUsername':'fixture', 'sshPassword':'synthetic'})
        with self.assertRaises(s.Problem):
            d.auth({'sshUsername':'fixture'})

    def test_preflight_does_not_contact_platform(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        result = {'service':'active', 'licensePresent':True, 'targetExists':False,
                  'locked':False, 'configPathSafe':True}
        with patch.object(d, 'connect', return_value=connection), patch.object(d, 'remote', side_effect=[json.dumps(result), '0']), patch.object(d, 'configs_for', return_value={'fixture':{'corridors_zf':[]}}), patch('httpx.Client') as platform:
            checked = d.preflight({'id':'fixture', 'options':{'projectName':'fixture'}}, {})
        self.assertEqual(checked['service'], 'active')
        self.assertNotIn('baseline', checked)
        platform.assert_not_called()

    def test_worker_monitors_recovery_without_platform_queries(self):
        from unittest.mock import MagicMock
        p = micro(True)
        j = d.prepare(p['id'], dict(revision=p['revision'], host='host'))
        j['remoteRun'] = '/synthetic'
        d.save(j)
        (d.folder(j['id'])/'auth.dpapi').write_bytes(s.crypt(b'{}'))
        connection = MagicMock()
        connection.__enter__.return_value = connection
        sftp = connection.open_sftp.return_value.__enter__.return_value
        states = [{'status':'running', 'startedAtMs':1},
                  {'status':'finished', 'startedAtMs':1, 'originalConfigRestored':True, 'serviceActive':'active'}]
        sftp.open.side_effect = [io.StringIO(json.dumps(state)) for state in states]
        with patch.object(d, 'connect', return_value=connection), patch('httpx.Client') as platform, patch.object(d.time, 'sleep'):
            d.worker(j['id'])
        platform.assert_not_called()
        saved = d.record(j['id'])
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(saved['verificationMode'], 'manual')
        self.assertFalse(saved['platformVerified'])
        self.assertNotIn('telemetry', saved)
        self.assertFalse((d.folder(j['id'])/'auth.dpapi').exists())

    def test_telemetry_route_removed(self):
        import server
        self.assertNotIn('/api/deployments/{jid}/telemetry', [route.path for route in server.app.routes])

    def test_status_write_recovers_from_transient_windows_file_lock(self):
        target=s.DATA/'status-retry.json';s.atomic(target,{'state':'running'})
        original=s.os.replace;attempts=[]
        def replace(a,b):
            attempts.append(1)
            if len(attempts)==1:raise PermissionError('sharing violation')
            original(a,b)
        with patch.object(s.os,'replace',side_effect=replace):s.atomic(target,{'state':'completed'})
        self.assertEqual(s.read(target)['state'],'completed')

    def test_subset_package_is_immutable_and_keeps_credentials_private(self):
        p=micro(True)
        before=copy.deepcopy(p)
        j=d.prepare(p['id'],{'revision':p['revision'],'host':'10.8.8.183','projectName':'test4'})
        self.assertEqual(s.get_project(p['id']),before)
        self.assertEqual(len(j['devices']),4)
        self.assertEqual(sum(x['points'] for x in j['devices']),10870)
        with zipfile.ZipFile(io.BytesIO(d.package(j['id']))) as z:
            cfg=json.loads(z.read('test4/tb_gateway.json'))
            self.assertEqual([name for name in z.namelist() if name.endswith('summary.md')],['summary.md'])
            summary=z.read('summary.md').decode('utf-8')
            self.assertIn('执行状态：prepared',summary)
            self.assertIn('平台上报：请以部署验收记录为准',summary)
            self.assertNotIn('fake-test-secret',summary)
        self.assertEqual(len(cfg['corridors_zf']),4)
        self.assertEqual(cfg['project_name'],'test4')
        self.assertTrue(all(x['server']['message_tool']=='message_platform' for x in cfg['corridors_zf']))
        self.assertNotIn('fixture-',json.dumps(j))
        self.assertFalse((d.folder(j['id'])/'auth.dpapi').exists())

    def test_invalid_paths_and_old_revision_rejected(self):
        for name in ['../test','test;whoami','/root','test\nstop']:
            with self.assertRaises(s.Problem):d.options({'host':'10.8.8.183','projectName':name})
        with self.assertRaises(s.Problem):d.options({'host':'a;whoami'})
        p=micro(True)
        with self.assertRaises(s.Problem):d.prepare(p['id'],{'revision':999,'host':'host'})

    def test_duplicate_start_does_not_deploy_twice(self):
        p=micro(True);j=d.prepare(p['id'],{'revision':p['revision'],'host':'host'})
        j['state']='completed';d.save(j)
        with patch.object(d,'preflight') as pre:
            result=d.start(j['id'],dict(sshUsername='u',sshPassword='p'))
            self.assertEqual(result['state'],'completed');pre.assert_not_called()

    def test_modified_project_prevents_deployment(self):
        p=micro(True);j=d.prepare(p['id'],{'revision':p['revision'],'host':'host'})
        p['name']='changed';s.save_project(p)
        with patch.object(d,'preflight') as pre:
            with self.assertRaises(s.Problem):d.start(j['id'],dict(sshUsername='u',sshPassword='p'))
            pre.assert_not_called()

    def test_api_requires_session_token(self):
        from fastapi.testclient import TestClient
        import server
        client=TestClient(server.app)
        self.assertEqual(client.post('/api/deployments/'+'a'*32+'/start',json={}).status_code,403)

    def test_remote_runner_restores_without_touching_existing_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()/'gateway';config=root/'thingsboard_gateway/config';config.mkdir(parents=True)
            existing=config/'test1';existing.mkdir();(existing/'tb_gateway.json').write_text('{"existing":true}')
            run=root/'run';run.mkdir();(run/'staged').mkdir();(run/'staged/tb_gateway.json').write_text('{"test":true}')
            (run/'settings.json').write_text(json.dumps({'projectName':'test4','durationSeconds':0}))
            text=(s.BASE/'remote_validation.py').read_text('utf-8').replace("ROOT = Path('/root/gateway')",'ROOT = Path('+repr(str(root))+')')
            (run/'runner.py').write_text(text,encoding='utf-8')
            commands=[]
            def fake_run(args,**kwargs):
                commands.append(args);return SimpleNamespace(stdout='active\n')
            with patch('subprocess.run',side_effect=fake_run):runpy.run_path(str(run/'runner.py'))
            state=json.loads((run/'status.json').read_text())
            self.assertTrue(state['originalConfigRestored'])
            self.assertFalse((config/'test4').exists())
            self.assertTrue((run/'completed-project/tb_gateway.json').is_file())
            self.assertEqual((existing/'tb_gateway.json').read_text(),'{"existing":true}')
            self.assertEqual([x[1] for x in commands],['stop','start','is-active','stop','start','is-active'])

    def test_upload_failure_does_not_claim_remote_running(self):
        p=micro(True);j=d.prepare(p['id'],{'revision':p['revision'],'host':'host'})
        (d.folder(j['id'])/'auth.dpapi').write_bytes(s.crypt(json.dumps(dict(sshUsername='u',sshPassword='p')).encode()))
        with patch.object(d,'connect',side_effect=ConnectionError):d.worker(j['id'],True)
        self.assertEqual(d.record(j['id'])['state'],'failed')
        self.assertFalse((d.folder(j['id'])/'auth.dpapi').exists())

    def test_formal_worker_uploads_to_config_and_keeps_project(self):
        p=micro(True)
        j=d.prepare(p['id'],{'revision':p['revision'],'host':'host','deploymentMode':'formal'})
        (d.folder(j['id'])/'auth.dpapi').write_bytes(s.crypt(json.dumps(dict(sshUsername='u',sshPassword='p')).encode()))
        paths=[]

        class Output:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def write(self,content): pass

        class Sftp:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def mkdir(self,path,*args): paths.append(path)
            def open(self,path,*args): paths.append(path);return Output()
            def chmod(self,*args): pass

        class Connection:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def open_sftp(self): return Sftp()

        completed={'status':'completed','startedAtMs':1234567890000,'projectNames':['test4'],'serviceActive':'active'}
        with patch.object(d,'connect',return_value=Connection()), patch.object(d,'remote',return_value=json.dumps(completed)) as execute:
            d.worker_formal(j['id'])
        self.assertEqual(d.record(j['id'])['state'],'completed')
        self.assertTrue(all(path.startswith('/root/gateway/thingsboard_gateway/config/.workbench-upload-') for path in paths))
        self.assertIn('def deploy(settings):',execute.call_args.args[1])
        self.assertFalse((d.folder(j['id'])/'auth.dpapi').exists())

    def test_formal_start_selects_persistent_worker(self):
        p=micro(True)
        j=d.prepare(p['id'],{'revision':p['revision'],'host':'host','deploymentMode':'formal'})
        preflight={'switchPlanId':'confirmed','switchPlan':{'retain':[],'existing':[]},'replacementHashes':{}}
        j['preflight']=preflight;d.save(j)
        with patch.object(d,'preflight',return_value=preflight), patch.object(d.threading,'Thread') as thread:
            result=d.start(j['id'],{'sshUsername':'u','sshPassword':'p','switchPlanId':'confirmed'})
        self.assertEqual(result['state'],'starting')
        self.assertIs(thread.call_args.kwargs['target'],d.worker_formal)

    def test_resume_closes_missing_remote_validation_only_when_gateway_healthy(self):
        p=micro(True)
        j=d.prepare(p['id'],{'revision':p['revision'],'host':'stale-host'})
        j.update(state='recovering',remoteRun='/root/gateway/workbench-validation-'+j['id'])
        d.save(j)
        auth=d.folder(j['id'])/'auth.dpapi'
        auth.write_bytes(s.crypt(json.dumps({'sshUsername':'u','sshPassword':'p'}).encode()))
        with patch.object(d,'connect'), patch.object(d,'remote',return_value=json.dumps({'run':False,'lock':False,'service':'active'})):
            result=d.resume(j['id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(auth.exists())

    def test_existing_remote_validation_remains_blocking(self):
        p=micro(True)
        j=d.prepare(p['id'],{'revision':p['revision'],'host':'active-host'})
        j.update(state='recovering',remoteRun='/root/gateway/workbench-validation-'+j['id'])
        d.save(j)
        with patch.object(d,'connect'), patch.object(d,'remote',return_value=json.dumps({'run':True,'lock':False,'service':'active'})):
            self.assertFalse(d.reconcile_stale_validation(j,{'sshUsername':'u','sshPassword':'p'}))
        self.assertEqual(d.record(j['id'])['state'],'recovering')


    def generated_fixture(self):
        from test_workspace import ready, wait_job
        p=ready()
        p['gateway']['maxProjectPoints']=1400
        s.save_project(p)
        plan=s.prepare(p['id'],p['revision'])
        job=wait_job(s.generate(p['id'],plan['planId'],'generated-deployment-test'))
        self.assertEqual(job['state'],'completed',job['message'])
        return p,job,plan['projects'][0]['name']

    def test_generated_storage_uses_selected_file_and_original_revision(self):
        p,job,member=self.generated_fixture()
        original=copy.deepcopy(p)
        p['revision']+=1;p['name']='new unsaved-to-package settings';s.save_project(p)
        j=d.prepare_generated(job['id'],dict(member=member,host='host',projectName='selected'))
        self.assertEqual(j['revision'],original['revision'])
        self.assertEqual(j['sourceMode'],'generated')
        with zipfile.ZipFile(s.DATA/'packages'/job['id']/'gateway-projects.zip') as z:
            expected=json.loads(z.read(member+'/tb_gateway.json'))
        expected['project_name']='selected'
        self.assertEqual(d.config_for(j['id']),expected)
        self.assertEqual(len(j['devices']),len(expected['corridors_zf']))
        import acceptance
        mapped=acceptance.query_keys_for(j,d.configs_for(j['id']))
        self.assertEqual(len(mapped),len(j['devices']))
        first=j['devices'][0]
        self.assertEqual(mapped[first['id']],first['expectedKeys'])
        self.assertNotEqual(first['expectedKeys'][0],'Tag1')
        self.assertEqual(s.get_project(p['id']),p)
        self.assertNotIn('fake-test-secret',json.dumps(j))
        self.assertIn(job['id'],[x['id'] for x in d.generated_sources()])

    def test_generated_batch_keeps_original_project_directories_by_default(self):
        _,job,_=self.generated_fixture()
        plan=s.read(s.DATA/'plans'/(job['planId']+'.json'))
        original_names=[row['name'] for row in plan['public']['projects']]
        self.assertGreater(len(original_names),1)
        prepared=d.prepare_generated(job['id'],{'member':'*','host':'host'})
        self.assertEqual(prepared['options']['projectNames'],original_names)
        self.assertEqual(prepared['options']['remotePaths'],
                         ['/root/gateway/thingsboard_gateway/config/'+name for name in original_names])
        with zipfile.ZipFile(io.BytesIO(d.package(prepared['id']))) as archive:
            self.assertEqual([name for name in archive.namelist() if name.endswith('/tb_gateway.json')],
                             [name+'/tb_gateway.json' for name in original_names])
            for name in original_names:
                self.assertEqual(json.loads(archive.read(name+'/tb_gateway.json'))['project_name'],name)

    def test_generated_rejects_bad_member_and_incomplete_job(self):
        p,job,member=self.generated_fixture()
        with self.assertRaises(s.Problem):
            d.prepare_generated(job['id'],dict(member='../bad',host='host'))
        job['state']='failed';s.note_job(job)
        with self.assertRaises(s.Problem):
            d.prepare_generated(job['id'],dict(member=member,host='host'))
        self.assertNotIn(job['id'],[x['id'] for x in d.generated_sources()])

    def test_generated_deleted_project_cannot_prepare(self):
        p,job,member=self.generated_fixture()
        s.set_project_deleted(p['id'],p['revision'],True)
        with self.assertRaises(s.Problem):
            d.prepare_generated(job['id'],dict(member=member,host='host'))


if __name__=='__main__':unittest.main()
