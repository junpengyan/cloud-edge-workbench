import csv
import io
import json
import unittest
import zipfile
from unittest.mock import patch, MagicMock
from test_workspace import s, ready, wait_job
from asset_templates import build_package


class TemplateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.vault = patch.object(s, 'crypt', side_effect=lambda data, decrypt=False: data)
        self.vault.start()
        self.addCleanup(self.vault.stop)

    def project(self, aggregate=False):
        p=s.new_project('synthetic template test')
        for row in p['types']:
            row.update(total=1,points=2)
        p['pointsConfirmed']=True
        p['gateway'].update(mqttHost='127.0.0.1',allowFill=True,forwardMode='aggregate' if aggregate else 'direct')
        s.save_project(p)
        return p

    def template_pool(self, aggregate=False):
        p=self.project(aggregate)
        p['cloud']['base_url']='http://fixture.invalid'
        s.save_project(p)
        return s.continue_with_templates(p['id'],p['revision'])

    def binding_client(self):
        client=MagicMock()
        client.__enter__.return_value=client
        client.find_device_id_by_name.side_effect=lambda name:'real-'+name
        client.get_device_credentials.side_effect=lambda ident:{'credentialsType':'MQTT_BASIC',
            'credentialsValue':json.dumps({'clientId':ident,'userName':'real-user','password':'synthetic-secret'})}
        def relations(ident,direction):
            if direction!='from': return []
            target={'real-test-bms1':'real-test-groupbms1','real-test-groupbms1':'real-test-cell1'}.get(ident)
            return [{'from':{'id':ident,'entityType':'DEVICE'},'to':{'id':target,'entityType':'DEVICE'}}] if target else []
        client.get_device_relations.side_effect=relations
        return client

    def bind(self,p,client):
        with patch('app.platform.thingsboard_client.ThingsBoardClient',return_value=client):
            return wait_job(s.bind_template_pool(p['id'],{'revision':p['revision'],'auth':{
                'existing_tenant_admin_username':'fixture','existing_tenant_admin_password':'fixture'}}))

    def test_template_pool_converts_with_shared_identities_and_allows_next_round(self):
        import rounds
        p=self.template_pool(True)
        # Names saved in the pool take precedence over the current naming prefix.
        p['cloud']['device_name_prefix']='changed-'
        p['gateway']['sharedDevicePrefix']='test-'
        s.save_project(p)
        client=self.binding_client()
        job=self.bind(p,client)
        self.assertEqual(job['state'],'completed',job['message'])
        bound=s.get_project(p['id'])
        self.assertFalse(bound['templateMode'])
        self.assertEqual(bound['sourceStatus'],'SUCCESS')
        self.assertFalse(bound['mappingIssues'])
        self.assertEqual(bound['revision'],p['revision']+1)
        self.assertTrue(all(b['platformId'].startswith('real-') and b['credentialStatus']!='template_unverified'
                            for b in bound['bindings']+bound['sharedBindings']))
        client.find_device_id_by_name.assert_any_call('test-pcs1')
        self.assertFalse(any(call[0].startswith(('create','ensure','save','delete')) for call in client.mock_calls))
        rounds.selection(bound['id'],{'revision':bound['revision'],'counts':{r['type']:r['total'] for r in bound['types']}})
        plan=s.prepare(bound['id'],bound['revision'])
        generated=wait_job(s.generate(bound['id'],plan['planId'],'converted-template'))
        self.assertEqual(generated['state'],'completed',generated['message'])

    def test_template_query_failure_is_atomic_and_can_retry(self):
        p=self.template_pool(True)
        client=self.binding_client()
        client.find_device_id_by_name.side_effect=lambda name:None if 'z' in name else 'real-'+name
        self.assertEqual(self.bind(p,client)['state'],'failed')
        self.assertEqual(s.get_project(p['id']),p)
        self.assertEqual(self.bind(p,self.binding_client())['state'],'completed')

    def test_template_conversion_preserves_devices_outside_current_selection(self):
        p=self.template_pool()
        next(r for r in p['types'] if r['type']=='PCS')['total']=0
        s.save_project(p)
        self.assertEqual(self.bind(p,self.binding_client())['state'],'completed')
        bound=s.get_project(p['id'])
        self.assertEqual(len(bound['bindings']),len(p['bindings']))
        self.assertTrue(any(b['platformName']=='test-pcs1' for b in bound['bindings']))
        self.assertEqual(bound['types'],p['types'])

    def test_missing_relations_still_require_manual_mapping_confirmation(self):
        p=self.template_pool()
        client=self.binding_client()
        client.get_device_relations.side_effect=None
        client.get_device_relations.return_value=[]
        self.assertEqual(self.bind(p,client)['state'],'completed')
        bound=s.get_project(p['id'])
        self.assertFalse(bound['templateMode'])
        self.assertTrue(bound['mappingIssues'])
        with self.assertRaises(s.Problem): s.prepare(bound['id'],bound['revision'])

    def test_template_duplicate_credentials_preserve_original_pool(self):
        p=self.template_pool()
        client=self.binding_client()
        client.get_device_credentials.side_effect=None
        client.get_device_credentials.return_value={'credentialsType':'MQTT_BASIC','credentialsValue':json.dumps({'clientId':'duplicate'})}
        self.assertEqual(self.bind(p,client)['state'],'failed')
        self.assertEqual(s.get_project(p['id']),p)

    def test_template_rebind_rejects_stale_revision_and_non_template_pool(self):
        p=self.template_pool()
        with self.assertRaises(s.Problem): s.bind_template_pool(p['id'],{'revision':p['revision']-1})
        bound=ready()
        with self.assertRaises(s.Problem): s.bind_template_pool(bound['id'],{'revision':bound['revision']})
        with self.assertRaises(s.Problem): s.provision(p['id'],{'revision':p['revision']})

    def test_template_query_does_not_overwrite_concurrent_configuration_change(self):
        p=self.template_pool()
        client=self.binding_client()
        def login(*args):
            current=s.get_project(p['id']);current['revision']+=1;current['name']='changed';s.save_project(current)
        client.login.side_effect=login
        self.assertEqual(self.bind(p,client)['state'],'failed')
        current=s.get_project(p['id'])
        self.assertEqual(current['bindings'],p['bindings'])
        self.assertEqual(current['name'],'changed')
        self.assertTrue(current['templateMode'])

    def test_failed_lookup_can_generate_one_zip_with_csv_and_gateway(self):
        p=self.project()
        s.note_job({'id':s.uid(),'projectId':p['id'],'kind':'cloud','reuseExistingDevices':True,'state':'failed','created':s.now()})
        p=s.continue_with_templates(p['id'],p['revision'])
        self.assertEqual(p['sourceStatus'],'TEMPLATE')
        self.assertTrue(all(b['platformId']=='' for b in p['bindings']))
        plan=s.prepare(p['id'],p['revision'])
        job=wait_job(s.generate(p['id'],plan['planId'],'template-test-key'))
        self.assertEqual(job['state'],'completed',job['message'])
        with zipfile.ZipFile(s.DATA/'packages'/job['id']/'gateway-projects.zip') as archive:
            self.assertTrue(any(n.endswith('/tb_gateway.json') for n in archive.namelist()))
            rows=list(csv.reader(io.StringIO(archive.read('asset-device-templates/1_pcs.csv').decode('utf-8-sig'))))
            self.assertEqual(rows[1][:5],['test-pcs1','pcs','test-pcs1','test-pcs1','test-pcs1'])
            self.assertEqual(rows[1][6],'')
            report=json.loads(archive.read('validation.json'))
            self.assertTrue(report['templateMode'])
            self.assertFalse(report['platformVerified'])
            self.assertIn('asset-device-templates/bms-groupbms_relation.csv',archive.namelist())

    def test_uncreated_devices_can_generate_and_template_counts_can_change(self):
        p=self.project()
        p=s.continue_with_templates(p['id'],p['revision'])
        self.assertEqual(p['sourceStatus'],'TEMPLATE')
        self.assertTrue(all(not b['platformId'] for b in p['bindings']))
        changed=[dict(row,total=2 if row['type']=='PCS' else row['total']) for row in p['types']]
        p=s.update(p['id'],{'revision':p['revision'],'name':p['name'],'types':changed,
                           'pointsConfirmed':True,'gateway':p['gateway'],'cloud':p['cloud']})
        p=s.continue_with_templates(p['id'],p['revision'])
        self.assertEqual(len([b for b in p['bindings'] if b['businessType']=='PCS']),2)
        plan=s.prepare(p['id'],p['revision'])
        job=wait_job(s.generate(p['id'],plan['planId'],'uncreated-template'))
        self.assertEqual(job['state'],'completed',job['message'])
        with zipfile.ZipFile(s.DATA/'packages'/job['id']/'gateway-projects.zip') as archive:
            self.assertIn('asset-device-templates/2_pcs.csv',archive.namelist())
        changed=[dict(row,total=1 if row['type']=='PCS' else row['total']) for row in p['types']]
        p=s.update(p['id'],{'revision':p['revision'],'name':p['name'],'types':changed,
                           'pointsConfirmed':True,'gateway':p['gateway'],'cloud':p['cloud']})
        self.assertEqual(len([b for b in p['bindings'] if b['businessType']=='PCS']),1)

    def test_success_preserves_real_credentials_and_names(self):
        p=ready()
        before=p['bindings']
        p=s.continue_with_templates(p['id'],p['revision'])
        self.assertEqual(p['bindings'],before)
        self.assertFalse(p['templateMode'])
        with zipfile.ZipFile(io.BytesIO(build_package(p,lambda b:s.get_secret(b['credentialRef'])))) as archive:
            rows=list(csv.reader(io.StringIO(archive.read('1_pcs.csv').decode('utf-8-sig'))))
            self.assertEqual(rows[1][:5],['fixture-pcs7','pcs','test-client-PCS','test-user-PCS','fake-test-secret-PCS'])

    def test_shared_channels_use_same_credentials_in_csv_and_gateway_input(self):
        p=self.project(True)
        p=s.continue_with_templates(p['id'],p['revision'])
        payload=s.payload_for(p)
        self.assertTrue(p['sharedBindings'])
        with zipfile.ZipFile(io.BytesIO(build_package(p,lambda b:s.get_secret(b['credentialRef'])))) as archive:
            name=next(n for n in archive.namelist() if n.endswith('_shared-channel.csv'))
            rows=list(csv.reader(io.StringIO(archive.read(name).decode('utf-8-sig'))))
            for row in rows[1:]:
                self.assertEqual(row[2:5],[row[0]]*3)
                self.assertIn(row[0],[c['client_id'] for c in payload['config']['identityMap'].values()])

    def test_stale_revision_and_running_lookup_block_continuation(self):
        p=self.project()
        with self.assertRaises(s.Problem): s.continue_with_templates(p['id'],p['revision']-1)
        job={'id':s.uid(),'projectId':p['id'],'state':'running','created':s.now()}
        s.note_job(job)
        try:
            with self.assertRaises(s.Problem): s.continue_with_templates(p['id'],p['revision'])
        finally:
            job['state']='failed';s.note_job(job)

    def test_microgrid_station_relations_and_generic_device_headers(self):
        p=s.new_project('microgrid template','microgrid')
        p['cloud']['asset_template']='电站,2,powerStation'
        for row in p['types']: row['total']=3
        with zipfile.ZipFile(io.BytesIO(build_package(p))) as archive:
            self.assertIn('2_asset-station.csv',archive.namelist())
            rows=list(csv.reader(io.StringIO(archive.read('ess_station_relation.csv').decode('utf-8-sig'))))
            self.assertEqual([r[1] for r in rows[1:]],['mg-电站1','mg-电站1','mg-电站2'])
            self.assertTrue(all(r[0]=='ASSET' and r[2]=='DEVICE' for r in rows[1:]))
            devices=list(csv.reader(io.StringIO(archive.read('3_ess.csv').decode('utf-8-sig'))))
            self.assertEqual(devices[1][2:5],['mg-ess1']*3)
