"""Business-flow tests use an isolated in-memory vault on non-Windows hosts."""
import copy
import io
import json
import os
import runpy
import secrets
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_workspace import s, ready, wait_job
from test_microgrid import micro
import rounds
import deployment as d


class RoundTests(unittest.TestCase):
    def setUp(self):
        if os.name != 'nt':
            vault = {}
            def crypt(blob, decrypt=False):
                if decrypt:
                    return vault[blob]
                key = secrets.token_bytes(32)
                vault[key] = blob
                return key
            self.patch = patch.object(s, 'crypt', side_effect=crypt)
            self.patch.start()
            self.addCleanup(self.patch.stop)

    def body(self, p, counts):
        return {'revision':p['revision'], 'name':'second round', 'counts':counts}

    def test_subset_keeps_pool_and_reuses_exact_identity(self):
        p = micro(True)
        before = copy.deepcopy(p)
        counts = {'PCS':1,'ESS':1,'INV':0,'BOX':1}
        body = self.body(p, counts)
        preview = rounds.preview(p['id'], body)
        self.assertEqual(preview['devices'], 3)
        r = rounds.create(p['id'], body)
        self.assertEqual(s.get_project(p['id']), before)
        original = {b['platformId']:b for b in p['bindings']}
        for b in r['bindings']:
            self.assertEqual(b['credentialRef'], original[b['platformId']]['credentialRef'])
        plan = s.prepare(r['id'],1)
        job = wait_job(s.generate(r['id'],plan['planId'],'round-subset-test'))
        self.assertEqual(job['state'],'completed',job['message'])
        dep = d.prepare_generated(job['id'],dict(member='*',host='host',projectName='round2',durationSeconds=600))
        self.assertEqual(dep['configuredDevices'],3)
        self.assertEqual(dep['poolDevices'],8)
        self.assertIsNone(dep['observedDevices'])
        with zipfile.ZipFile(io.BytesIO(d.package(dep['id']))) as archive:
            clients = {c['server']['client_id'] for name in archive.namelist() if name.endswith('tb_gateway.json') for c in json.loads(archive.read(name))['corridors_zf']}
        self.assertEqual(clients, {'fixture-PCS1','fixture-ESS1','fixture-BOX1'})
        # Creating from a small round can select the full original pool again.
        third = rounds.create(r['id'],self.body(r,dict.fromkeys(counts,2)))
        self.assertEqual(len(third['bindings']),8)
        self.assertEqual(third['testRound']['poolRevision'],p['revision'])
        with self.assertRaises(s.Problem):
            d.prepare_generated(job['id'],dict(member=plan['projects'][0]['name'],host='host'))

    def test_counts_versions_and_complete_stacks(self):
        p = ready()
        counts = {r['type']:r['total'] for r in p['types']}
        counts['CELL'] = 0
        with self.assertRaises(s.Problem):rounds.create(p['id'],self.body(p,counts))
        counts.update(BMS=0,GROUPBMS=0)
        selected = rounds.create(p['id'],self.body(p,counts))
        self.assertEqual(len(selected['bindings']),3)
        counts['PCS'] = 2
        with self.assertRaises(s.Problem):rounds.create(p['id'],self.body(p,counts))
        counts['PCS'] = True
        with self.assertRaises(s.Problem):rounds.create(p['id'],self.body(p,counts))
        with self.assertRaises(s.Problem):rounds.create(p['id'],dict(revision=999,counts=counts))

    def test_whole_batch_deploys_every_split(self):
        p=ready();p['gateway']['maxProjectPoints']=1400;s.save_project(p)
        r=rounds.create(p['id'],self.body(p,{row['type']:row['total'] for row in p['types']}))
        plan=s.prepare(r['id'],1)
        self.assertGreater(len(plan['projects']),1)
        job=wait_job(s.generate(r['id'],plan['planId'],'round-whole-batch'))
        dep=d.prepare_generated(job['id'],dict(member='*',host='host',projectName='batch'))
        self.assertEqual(len(d.configs_for(dep['id'])),len(plan['projects']))
        self.assertEqual(len(dep['devices']),len(r['bindings']))
        with self.assertRaises(s.Problem): d.assessment(dep['id'],dict(observedDevices=5,notes='人工检查'))
        dep['remoteStatus']={'startedAtMs':1};d.save(dep)
        observed=d.assessment(dep['id'],dict(observedDevices=5,notes='人工检查'))
        self.assertEqual(observed['observedDevices'],5)
        self.assertFalse(observed['platformVerified'])
        with self.assertRaises(s.Problem):d.assessment(dep['id'],dict(observedDevices=7))

    def test_shared_round_batch_keeps_channel_and_business_identities_separate(self):
        p=ready();p['gateway'].update(forwardMode='aggregate',maxProjectPoints=1400,sharedDevicePrefix='round-')
        p['sharedBindings']=[]
        for item in s.shared_device_specs(p):
            cred={'client_id':'connection-'+item['platformName'],'user_name':item['platformName'],'pass_word':'fixture'}
            p['sharedBindings'].append({**item,'platformId':'channel-'+item['platformName'],
                'platformUrl':p['cloud']['base_url'].rstrip('/'),'credentialRef':s.put_secret(cred),'credentialsPending':False})
        s.save_project(p)
        r=rounds.create(p['id'],self.body(p,{row['type']:row['total'] for row in p['types']}))
        plan=s.prepare(r['id'],r['revision'])
        job=wait_job(s.generate(r['id'],plan['planId'],'round-shared-batch'))
        self.assertEqual(job['state'],'completed',job['message'])
        dep=d.prepare_generated(job['id'],dict(member='*',host='host',projectName='sharedround'))
        self.assertEqual({x['id'] for x in dep['devices']},{b['platformId'] for b in r['bindings']})
        configs=d.configs_for(dep['id'])
        channels=[c for cfg in configs.values() for c in cfg['corridors_zf']]
        self.assertTrue(all(c['server']['client_id'].startswith('connection-') for c in channels))
        self.assertEqual(dep['configuredPoints'],sum(row['total']*row['points'] for row in r['types']))
        self.assertEqual(dep['forwardChannels'],len(channels))

    def test_ten_thousand_to_nine_thousand_and_eight_thousand(self):
        p=micro(True)
        prototype=p['bindings'][0]
        p['bindings']=[dict(prototype,platformId='id-'+str(i),platformName='pcs'+str(i),logicalId='logical-'+str(i),collectAlias='pcs'+str(i),businessType='PCS') for i in range(1,10001)]
        for row in p['types']:row['total']=10000 if row['type']=='PCS' else 0
        s.save_project(p)
        counts={'PCS':9000,'ESS':0,'INV':0,'BOX':0}
        r=rounds.create(p['id'],self.body(p,counts))
        self.assertEqual(len(r['bindings']),9000)
        self.assertEqual(r['bindings'][-1]['platformId'],'id-9000')
        counts['PCS']=8000
        r2=rounds.create(r['id'],self.body(r,counts))
        self.assertEqual(len(r2['bindings']),8000)
        self.assertEqual(len(s.get_project(p['id'])['bindings']),10000)
        self.assertEqual(r2['testRound']['poolDevices'],10000)

    def test_refresh_pool_is_read_only_and_preserves_round_snapshot(self):
        p=micro(True)
        r=rounds.create(p['id'],self.body(p,dict(PCS=1,ESS=0,INV=0,BOX=0)))
        old=s.get_secret(r['bindings'][0]['credentialRef'])
        from unittest.mock import MagicMock
        client=MagicMock()
        client.get_device_credentials.side_effect=lambda device_id: {'credentialsType':'ACCESS_TOKEN','credentialsId':'token-'+device_id}
        with patch('app.platform.thingsboard_client.ThingsBoardClient',return_value=client):
            refreshed=rounds.refresh_pool(p['id'],dict(revision=p['revision'],auth={'existing_tenant_admin_username':'test','existing_tenant_admin_password':'test'}))
        self.assertEqual(refreshed['revision'],p['revision']+1)
        self.assertEqual(s.get_secret(r['bindings'][0]['credentialRef']),old)
        client.create_device.assert_not_called()
        client.set_mqtt_basic_credentials.assert_not_called()
        self.assertEqual(client.get_device_credentials.call_count,8)

    def test_duration_and_replacement_validation(self):
        for duration in (0, 9, True, 604801):
            with self.assertRaises(s.Problem):d.options(dict(host='host',durationSeconds=duration))
        for names in (['../old'],['old','old'],'old'):
            with self.assertRaises(s.Problem):d.options(dict(host='host',replaceProjects=names))

    def test_overview_groups_rounds_without_bindings_or_credentials(self):
        p=micro(True)
        r=rounds.create(p['id'],self.body(p,dict(PCS=1,ESS=1,INV=0,BOX=1)))
        summary=next(row for row in rounds.overview() if row['id']==r['id'])
        self.assertEqual(summary['testRound']['poolId'],p['id'])
        self.assertEqual(summary['boundDevices'],3)
        self.assertNotIn('bindings',summary)
        self.assertNotIn('selectedIds',summary['testRound'])
        self.assertNotIn('credentialRef',json.dumps(summary))
        self.assertIsNone(summary['generation'])

    def test_overview_distinguishes_old_generation_from_current_revision(self):
        p=micro(True)
        plan=s.prepare(p['id'],p['revision'])
        job=wait_job(s.generate(p['id'],plan['planId'],'overview-revision-test'))
        self.assertEqual(job['state'],'completed')
        self.assertTrue(next(r for r in rounds.overview() if r['id']==p['id'])['generation']['current'])
        s.update(p['id'],copy.deepcopy(p))
        self.assertFalse(next(r for r in rounds.overview() if r['id']==p['id'])['generation']['current'])

    def test_previous_deployment_requires_same_host_and_recovered_state(self):
        p=micro(True)
        previous=d.prepare(p['id'],dict(revision=p['revision'],host='gateway',projectName='previous'))
        previous['state']='completed';d.save(previous)
        options=d.options(dict(host='gateway',previousDeploymentId=previous['id']))
        self.assertEqual(options['replaceProjects'],['previous'])
        with self.assertRaises(s.Problem):d.options(dict(host='other',previousDeploymentId=previous['id']))
        with self.assertRaises(s.Problem):d.options(dict(host='gateway',previousDeploymentId=previous['id'],replaceProjects=['manual']))
        previous['state']='recovering';d.save(previous)
        with self.assertRaises(s.Problem):d.options(dict(host='gateway',previousDeploymentId=previous['id']))

    def test_replacement_change_requires_another_confirmation(self):
        p=micro(True)
        j=d.prepare(p['id'],dict(revision=p['revision'],host='isolated-host',replaceProjects=['old']))
        j['preflight']={'replacementHashes':{'old':'original'}};d.save(j)
        with patch.object(d,'preflight',return_value={'replacementHashes':{'old':'changed'}}),patch.object(d.threading,'Thread') as thread:
            with self.assertRaises(s.Problem):d.start(j['id'],dict(sshUsername='u',sshPassword='p'))
            thread.assert_not_called()
        self.assertEqual(d.record(j['id'])['state'],'prepared')


class RunnerTests(unittest.TestCase):
    def run_round(self, fail_start=False, changed=False, tamper=False, inventory_changed=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()/'gateway';config=root/'thingsboard_gateway/config';config.mkdir(parents=True)
            old=config/'old';old.mkdir();(old/'tb_gateway.json').write_text('{"corridors_zf":[]}')
            unrelated=config/'other';unrelated.mkdir();(unrelated/'tb_gateway.json').write_text('{"unrelated":true}')
            run=root/'run';run.mkdir();(run/'staged').mkdir()
            for name in ('new_1','new_2'):
                (run/'staged'/name).mkdir();(run/'staged'/name/'tb_gateway.json').write_text('{"corridors_zf":[]}')
            import hashlib
            settings=dict(projectName='new',projectNames=['new_1','new_2'],replaceProjects=['old'],durationSeconds=0,
                          replacementHashes={'old':hashlib.sha256((old/'tb_gateway.json').read_bytes()).hexdigest()})
            if inventory_changed:settings['inventoryHashes']={'old':'stale'}
            if changed:settings['replacementHashes']['old']='changed'
            if tamper:settings['durationSeconds']=10
            (run/'settings.json').write_text(json.dumps(settings))
            source=(s.BASE/'remote_validation.py').read_text().replace("ROOT = Path('/root/gateway')",'ROOT = Path('+repr(str(root))+')')
            (run/'runner.py').write_text(source)
            starts=[]
            def command(args,**kwargs):
                if args[1]=='start':
                    starts.append(1)
                    if fail_start and len(starts)==1:raise RuntimeError('start_failed')
                return SimpleNamespace(stdout='active\n')
            def sleep(_):
                if tamper:
                    (config/'new_1/tb_gateway.json').write_text('{"externally_changed":true}')
                    (run/'stop-request').write_text('stop')
            with patch('subprocess.run',side_effect=command), patch('time.sleep',side_effect=sleep):
                runpy.run_path(str(run/'runner.py'))
            state=json.loads((run/'status.json').read_text())
            self.assertEqual((unrelated/'tb_gateway.json').read_text(),'{"unrelated":true}')
            if tamper:
                self.assertEqual(state['status'],'recovery_required')
                self.assertTrue((root/'.workbench-validation-lock').exists())
                self.assertEqual((config/'new_1/tb_gateway.json').read_text(),'{"externally_changed":true}')
                return
            self.assertFalse((config/'new_1').exists());self.assertFalse((config/'new_2').exists())
            self.assertFalse((root/'.workbench-validation-lock').exists())
            if fail_start or changed or inventory_changed:
                self.assertTrue(old.exists());self.assertTrue(state['rollbackRestored'])
                self.assertFalse(state.get('originalConfigRestored',False))
            else:
                self.assertFalse(old.exists());self.assertTrue((run/'previous-projects/old').exists())
                self.assertTrue((run/'completed-projects/new_1').exists());self.assertTrue(state['originalConfigRestored'])

    def test_success_archives_both_rounds_and_preserves_other_projects(self):self.run_round()
    def test_start_failure_rolls_back_old_round(self):self.run_round(fail_start=True)
    def test_changed_old_config_blocks_switch(self):self.run_round(changed=True)

    def test_inventory_change_under_remote_lock_blocks_switch(self):self.run_round(inventory_changed=True)

    def test_external_change_keeps_configuration_and_requires_recovery(self):self.run_round(tamper=True)
