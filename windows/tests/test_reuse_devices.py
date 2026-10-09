import copy
import json
import unittest
from unittest.mock import MagicMock, patch
from test_workspace import s, wait_job
from app.models.config import RunConfig
from app.models.entities import RunResult
from app.core.orchestrator import QuickDataOrchestrator
from app.services.validation import build_run_validation


class ReuseDevicesTests(unittest.TestCase):
    def config(self):
        return RunConfig(
            base_url='http://test.invalid',
            existing_tenant_admin_username='fixture',
            existing_tenant_admin_password='fixture',
            device_name_prefix='fixture-',
            device_template='pcs,2,default',
            reuse_existing_devices=True, enable_device_creation=False,
            enable_rule_chain_creation=False, enable_edge_creation=False,
            enable_edge_assignment=False, enable_telemetry=False)

    def client(self):
        client=MagicMock()
        client.find_device_id_by_name.side_effect=lambda name:'id-'+name
        client.get_device_credentials.side_effect=lambda ident:{
            'credentialsType':'MQTT_BASIC',
            'credentialsValue':json.dumps({'clientId':ident,'userName':'existing-user','password':'existing-secret'})}
        client.get_device_relations.return_value=[]
        return client

    def test_reuse_reads_credentials_without_any_resource_writes(self):
        config=self.config();runner=QuickDataOrchestrator(config);client=self.client()
        result=RunResult(run_id='reuse')
        with patch.object(runner,'_precheck'), patch.object(runner,'_prepare_tenant_context',return_value=client), patch.object(runner,'_create_assets') as assets, patch.object(runner,'_create_relations_from_rules') as relations:
            runner._run_steps(result)
        self.assertEqual([d.name for d in result.devices],['fixture-pcs1','fixture-pcs2'])
        self.assertEqual(result.devices[0].mqtt_username,'existing-user')
        client.create_device.assert_not_called()
        client.ensure_device_mqtt_credentials.assert_not_called()
        client.delete_device.assert_not_called()
        assets.assert_not_called()
        relations.assert_not_called()
        check=next(c for c in build_run_validation(result,config) if c.category=='设备')
        self.assertTrue(check.success)
        self.assertEqual(check.name,'已有设备匹配数量')

    def test_missing_device_stops_before_resource_writes(self):
        runner=QuickDataOrchestrator(self.config());client=self.client()
        client.find_device_id_by_name.side_effect=['id-first','']
        result=RunResult(run_id='missing')
        with patch.object(runner,'_precheck'),patch.object(runner,'_prepare_tenant_context',return_value=client),patch.object(runner,'_create_assets') as assets:
            with self.assertRaisesRegex(ValueError,'未找到'):
                runner._run_steps(result)
        assets.assert_not_called()
        client.create_device.assert_not_called()
        self.assertEqual(result.devices,[])

    def test_duplicate_credentials_rejected_before_writes(self):
        runner=QuickDataOrchestrator(self.config());client=self.client()
        client.get_device_credentials.side_effect=None
        client.get_device_credentials.return_value={'credentialsType':'MQTT_BASIC','credentialsValue':'{"clientId":"duplicate"}'}
        with self.assertRaisesRegex(ValueError,'重复'):
            runner._load_existing_devices(client,RunResult(run_id='duplicate'))

    def test_reuse_binds_existing_ids_and_generates_gateway(self):
        p=s.new_project('reuse fixture')
        p['types'][0].update(total=2,points=290)
        p['pointsConfirmed']=True
        p['gateway']['mqttHost']='localhost'
        p['cloud'].update(base_url='http://test.invalid',device_name_prefix='fixture-')
        s.save_project(p)
        runner=QuickDataOrchestrator(self.config())
        result=RunResult(run_id='run-reused',status='SUCCESS')
        runner._load_existing_devices(self.client(),result)
        with patch('app.core.orchestrator.QuickDataOrchestrator') as factory:
            factory.return_value.run.return_value=result
            job=s.provision(p['id'],{'revision':1,'reuseExistingDevices':True,'auth':{
                'existing_tenant_admin_username':'fixture','existing_tenant_admin_password':'fixture'}})
            job=wait_job(job)
            self.assertEqual(job['state'],'completed',job['message'])
            cfg=factory.call_args.args[0]
            self.assertTrue(cfg.reuse_existing_devices)
            self.assertFalse(cfg.enable_device_creation)
            self.assertFalse(cfg.enable_asset_creation)
            self.assertFalse(cfg.enable_relation_creation)
            self.assertFalse(cfg.enable_edge_creation)
            self.assertFalse(cfg.enable_edge_assignment)
            self.assertFalse(cfg.enable_rule_chain_creation)
        bound=s.get_project(p['id'])
        self.assertEqual([b['platformId'] for b in bound['bindings']],['id-fixture-pcs1','id-fixture-pcs2'])
        self.assertNotIn('existing-secret',json.dumps(bound))
        plan=s.prepare(bound['id'],bound['revision'])
        generated=wait_job(s.generate(bound['id'],plan['planId'],'reuse-regression'))
        self.assertEqual(generated['state'],'completed',generated['message'])
        self.assertEqual(generated['summary']['totalDevices'],2)

    def test_existing_relations_are_loaded_once_and_not_created(self):
        from app.models.entities import EntityRecord
        runner=QuickDataOrchestrator(self.config())
        client=self.client()
        result=RunResult(run_id='relations',devices=[
            EntityRecord(entity_type='DEVICE',id='bms',name='bms1'),
            EntityRecord(entity_type='DEVICE',id='group',name='groupbms1')])
        relation={'from':{'id':'bms','entityType':'DEVICE'},'to':{'id':'group','entityType':'DEVICE'},
                  'type':'Contains','typeGroup':'COMMON','additionalInfo':{'type':'GroupBMS'}}
        client.get_device_relations.return_value=[relation]
        runner._load_existing_relations(client,result)
        self.assertEqual(len(result.relations),1)
        self.assertEqual(result.relations[0].from_id,'bms')
        self.assertEqual(result.relations[0].to_id,'group')
        client.create_relation.assert_not_called()

    def test_readonly_short_circuits_even_when_write_flags_are_enabled(self):
        config=self.config()
        config.enable_asset_creation=True
        config.enable_relation_creation=True
        config.enable_rule_chain_creation=True
        config.enable_edge_creation=True
        config.enable_edge_assignment=True
        config.enable_telemetry=True
        runner=QuickDataOrchestrator(config)
        client=self.client()
        with patch.object(runner,'_precheck'),patch.object(runner,'_prepare_tenant_context',return_value=client),patch.object(runner,'_publish_mqtt') as publish:
            runner._run_steps(RunResult(run_id='readonly'))
        publish.assert_not_called()
        writes=[call for call in client.mock_calls if any(word in call[0] for word in ('create','assign','set_','ensure_','delete','save_'))]
        self.assertEqual(writes,[])

    def test_relation_query_is_get_only(self):
        from app.platform.thingsboard_client import ThingsBoardClient
        client=ThingsBoardClient('http://test.invalid')
        try:
            with patch.object(client,'_request',return_value=[]) as request:
                client.get_device_relations('fixture-id','from')
                client.get_device_relations('fixture-id','to')
                self.assertEqual([c.args[0] for c in request.call_args_list],['GET','GET'])
                self.assertIn('fromType=DEVICE',request.call_args_list[0].args[1])
        finally:
            client.close()
