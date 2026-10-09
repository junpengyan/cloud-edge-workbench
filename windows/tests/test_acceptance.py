"""Offline coverage of round sizing, immutable execution and acceptance evidence."""
import copy
import json
import time
import unittest
from unittest.mock import MagicMock, patch
from test_workspace import s
from test_microgrid import micro
import test_rounds
import rounds
import deployment as d
import acceptance as a


class AcceptanceTests(unittest.TestCase):
    setUp = test_rounds.RoundTests.setUp

    def deployment(self, **options):
        p = micro(True)
        return d.prepare(p['id'],dict(revision=p['revision'],host='acceptance-host',durationSeconds=60,**options))

    def started(self, **options):
        j=self.deployment(**options)
        j['remoteStatus']={'startedAtMs':100000,'endedAtMs':160000}
        j['state']='completed';d.save(j)
        return j

    def test_total_allocation_caps_inventory_and_preserves_pool(self):
        p=micro(True); before=copy.deepcopy(p)
        result=rounds.allocate(p['id'],dict(revision=p['revision'],targetDevices=7))
        self.assertEqual(sum(result['counts'].values()),7)
        self.assertTrue(all(v<=2 for v in result['counts'].values()))
        self.assertEqual(s.get_project(p['id']),before)
        with self.assertRaises(s.Problem):rounds.allocate(p['id'],dict(revision=p['revision'],targetDevices=9))

    def test_whole_stack_allocation_finds_feasible_total(self):
        from test_workspace import ready
        p=ready()
        for target in range(1,7):
            result=rounds.allocate(p['id'],dict(revision=p['revision'],targetDevices=target))
            self.assertEqual(result['actualDevices'],target)
            self.assertEqual(len({result['counts'][k] for k in ('BMS','GROUPBMS','CELL')}),1)
        p['bindings']=[b for b in p['bindings'] if b['businessType'] in ('BMS','GROUPBMS','CELL')];s.save_project(p)
        result=rounds.allocate(p['id'],dict(revision=p['revision'],targetDevices=2))
        self.assertEqual(result['actualDevices'],0)
        with self.assertRaises(s.Problem):rounds.create(p['id'],dict(revision=p['revision'],counts=result['counts']))

    def test_latest_pool_refresh_diff_and_stale_preview_rejection(self):
        p=micro(True);counts=dict(PCS=1,ESS=1,INV=0,BOX=0)
        r=rounds.create(p['id'],dict(revision=p['revision'],counts=counts))
        p['revision']+=1;p['bindings'][0]['credentialRef']=s.put_secret({'client_id':'new','user_name':'new','pass_word':'synthetic'});s.save_project(p)
        body=dict(revision=r['revision'],counts=counts,poolMode='latest',poolRevision=p['revision'])
        preview=rounds.preview(r['id'],body)
        self.assertEqual(preview['changes']['credentialsChanged'],1)
        new=rounds.create(r['id'],body)
        self.assertEqual(new['testRound']['poolRevision'],p['revision'])
        self.assertNotEqual(new['bindings'][0]['credentialRef'],r['bindings'][0]['credentialRef'])
        p['revision']+=1;s.save_project(p)
        with self.assertRaises(s.Problem):rounds.create(r['id'],body)
        old=rounds.create(r['id'],dict(revision=r['revision'],counts=counts))
        self.assertEqual(old['bindings'][0]['credentialRef'],r['bindings'][0]['credentialRef'])

    def test_window_excludes_warmup_and_early_stop_is_incomplete(self):
        j=self.started(warmupSeconds=20)
        self.assertEqual(a.window(j,170000),dict(startTs=120000,endTs=160000,complete=True))
        with self.assertRaises(s.Problem):a.window(j,110000)
        j['remoteStatus']['endedAtMs']=140000
        self.assertFalse(a.window(j,170000)['complete'])
        for body in ({'warmupSeconds':60},{'warmupSeconds':55},{'criteria':{'maxLossPercent':101}}):
            with self.assertRaises(s.Problem):d.options(dict(host='host',durationSeconds=60,**body))

    def test_manifest_is_fixed_and_stale_worker_preserves_assessment(self):
        j=self.started(); frozen=copy.deepcopy(j['executionManifest']); stale=copy.deepcopy(j)
        d.assessment(j['id'],dict(observedDevices=3,notes='synthetic measurement',metrics={'ratePerSecond':25}))
        stale['state']='completed';d.save(stale)
        current=d.record(j['id'])
        self.assertEqual(current['measuredMetrics']['ratePerSecond'],25)
        self.assertEqual(current['executionManifest'],frozen)
        self.assertEqual(current['assessmentHistory'][0]['manifestId'],j['manifestId'])
        self.assertNotIn('pass_word',json.dumps(frozen))

    def test_thresholds_require_complete_evidence_and_manual_metrics(self):
        j=self.started(criteria={'minReportingPercent':100,'maxP95LatencyMs':100})
        j['telemetryCheck']=dict(state='completed',manifestId=j['manifestId'],reportingPercent=100,errors=0,window={'complete':True})
        self.assertEqual(a.verdict(j)['state'],'incomplete')
        j['measuredMetrics']={'p95LatencyMs':101};j['measurementWindow']={'complete':True}
        self.assertEqual(a.verdict(j)['state'],'failed')
        j['measuredMetrics']['p95LatencyMs']=90
        self.assertEqual(a.verdict(j)['state'],'passed')
        j['telemetryCheck']['errors']=1
        self.assertEqual(a.verdict(j)['state'],'incomplete')

    def test_telemetry_reports_missing_and_query_failure_separately(self):
        j=self.started(); a.ACTIVE.add(j['id'])
        self.addCleanup(a.ACTIVE.discard,j['id'])
        j['telemetryCheck']=dict(state='running',manifestId=j['manifestId'],window=a.window(j),total=4)
        d.save(j); client=MagicMock();client.__enter__.return_value=client
        ids=[device['id'] for device in j['devices']]
        from urllib.parse import unquote,parse_qs,urlsplit
        def query(method,path):
            self.assertEqual(method,'GET')
            device_id=unquote(path.split('/DEVICE/')[1].split('/')[0])
            key=parse_qs(urlsplit(path).query)['keys'][0].split(',')[0]
            if device_id==ids[0]:return {key:[{'ts':130000,'value':'1'}]}
            if device_id==ids[1]:return {key:[{'ts':99000,'value':'old'}]}
            if device_id==ids[2]:raise RuntimeError('synthetic failed query')
            return {}
        client._request.side_effect=query
        with patch('app.platform.thingsboard_client.ThingsBoardClient',return_value=client):
            a.worker(j['id'],'fixture','synthetic',a.window(j))
        current=d.record(j['id']);result=current['telemetryCheck']
        self.assertEqual((result['reported'],result['missing'],result['errors']),(1,2,1))
        self.assertEqual(current['acceptanceResult']['state'],'incomplete')
        self.assertFalse(current['platformVerified'])
        self.assertNotIn('synthetic',json.dumps(current))
        self.assertEqual(current['telemetryHistory'][0]['window'],a.window(j))

    def test_legacy_tag_keys_query_outgoing_telemetry_names(self):
        j=self.started()
        config=d.config_for(j['id'])
        member=config['corridors_zf'][0]['server']['devices'][0]
        actual_key=member['point_table'][0]['customize_info']
        self.assertNotEqual(actual_key,member['point_table'][0]['tag'])
        self.assertEqual(j['devices'][0]['expectedKeys'][0],actual_key)
        j['devices'][0]['expectedKeys']=['Tag1']
        a.freeze(j,{j['options']['projectName']:config})
        j['telemetryCheck']=dict(state='running',manifestId=j['manifestId'],window=a.window(j),total=len(j['devices']))
        d.save(j)
        client=MagicMock();client.__enter__.return_value=client
        seen=[]
        from urllib.parse import parse_qs,urlsplit
        def query(method,path):
            keys=parse_qs(urlsplit(path).query)['keys'][0].split(',')
            seen.extend(keys)
            return {actual_key:[{'ts':130000,'value':'1'}]} if actual_key in keys else {}
        client._request.side_effect=query
        a.ACTIVE.add(j['id']);self.addCleanup(a.ACTIVE.discard,j['id'])
        with patch('app.platform.thingsboard_client.ThingsBoardClient',return_value=client):
            a.worker(j['id'],'fixture','synthetic',a.window(j))
        self.assertIn(actual_key,seen)
        self.assertEqual(d.record(j['id'])['telemetryCheck']['reported'],1)

    def test_frozen_outgoing_keys_do_not_load_gateway_config(self):
        j=self.started()
        with patch.object(d,'configs_for',side_effect=AssertionError('gateway config should not be read')):
            keys=a.keys_for_check(j,j['id'])
        self.assertEqual(keys[j['devices'][0]['id']],j['devices'][0]['expectedKeys'])

    def test_login_failure_identifies_stage_without_exposing_credentials(self):
        from app.platform.thingsboard_client import ThingsBoardError
        j=self.started();j['telemetryCheck']=dict(state='running',manifestId=j['manifestId'],window=a.window(j),total=len(j['devices']))
        d.save(j);a.ACTIVE.add(j['id']);self.addCleanup(a.ACTIVE.discard,j['id'])
        client=MagicMock();client.__enter__.return_value=client
        client.login.side_effect=ThingsBoardError('POST /api/auth/login returned 401: synthetic-secret')
        with patch('app.platform.thingsboard_client.ThingsBoardClient',return_value=client):
            a.worker(j['id'],'fixture','synthetic-secret',a.window(j))
        result=d.record(j['id'])['telemetryCheck']
        self.assertEqual(result['failureStage'],'login')
        self.assertIn('HTTP 401',result['message'])
        self.assertNotIn('synthetic-secret',json.dumps(result))

    def test_interrupted_telemetry_can_be_retried(self):
        j=self.started();j['telemetryCheck']={'state':'running'};d.save(j)
        self.assertEqual(d.record(j['id'])['telemetryCheck']['state'],'failed')
        with patch.object(a.threading,'Thread'):
            try:
                result=a.start_check(j['id'],dict(username='u',password='p'))
                self.assertEqual(result['telemetryCheck']['state'],'running')
            finally:a.ACTIVE.discard(j['id'])

    def test_inventory_change_and_retained_ack_block_start(self):
        j=self.deployment()
        plan={'existing':[{'name':'other','hash':'a'}],'retain':['other']}
        pre={'switchPlan':plan,'switchPlanId':s.digest(plan),'replacementHashes':{}}
        j['preflight']=pre;d.save(j)
        with patch.object(d,'preflight',return_value=pre),patch.object(d.threading,'Thread') as thread:
            with self.assertRaises(s.Problem):d.start(j['id'],dict(sshUsername='u',sshPassword='p',switchPlanId=pre['switchPlanId']))
            with self.assertRaises(s.Problem):d.start(j['id'],dict(sshUsername='u',sshPassword='p',switchPlanId='stale',acknowledgeRetained=True))
            thread.assert_not_called()

    def test_retained_same_device_blocks_even_with_different_client(self):
        j=self.deployment()
        remote=dict(errors=[],replacementHashes={},inventory=[dict(name='old',hash='a',devices=1,deviceNames=[j['devices'][0]['name']],known=True)])
        with patch.object(d,'connect',return_value=MagicMock()),patch.object(d,'remote',return_value=json.dumps(remote)):
            with self.assertRaisesRegex(s.Problem,'仍包含本轮设备'):d.preflight(j,{})

    def test_modified_deployment_config_rejected_before_connecting(self):
        j=self.deployment();config=d.config_for(j['id']);config['project_name']='tampered'
        (d.folder(j['id'])/'config.dpapi').write_bytes(s.crypt(json.dumps(config).encode()))
        with patch.object(d,'preflight') as pre:
            with self.assertRaisesRegex(s.Problem,'部署文件已变化'):d.start(j['id'],dict(sshUsername='u',sshPassword='p'))
            pre.assert_not_called()
