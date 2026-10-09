import copy, json, zipfile, unittest
from collections import Counter
from unittest.mock import patch
from test_workspace import s, wait_job
import microgrid as mg

def micro(bound=False):
    p=s.new_project('microgrid-test','microgrid')
    p['gateway'].update(mqttHost='127.0.0.1',allowFill=True)
    p['pointsConfirmed']=True
    p['microgrid']['stationCount']=3
    p['cloud']['asset_template']='电站,3,powerStation'
    for r in p['types']: r['total']=2
    if bound:
        doc={'devices':[{'id':k+str(i),'name':'mg-'+k.lower()+str(i),'notes':'template='+k.lower(),
                        'mqtt_client_id':'fixture-'+k+str(i)} for k in mg.KINDS for i in (1,2)]}
        p['bindings'],p['relations'],p['mappingIssues']=s.build_bindings(doc,'microgrid')
        p['sourceStatus']='SUCCESS'
    s.save_project(p)
    return p

class MicrogridTests(unittest.TestCase):
    def test_presets_and_storage_isolation(self):
        p=s.new_project(profile='microgrid')
        self.assertEqual([r['type'] for r in p['types']],list(mg.KINDS))
        self.assertEqual(sum(r['total'] for r in p['types']),942)
        self.assertEqual(sum(r['total']*r['points'] for r in p['types']),2000434)
        self.assertEqual(sum(r['total']*r['fastPoints'] for r in p['types']),300264)
        self.assertEqual([r['type'] for r in s.new_project()['types']],list(s.KINDS))
        self.assertFalse(p['pointsConfirmed'])
        self.assertFalse(p['gateway']['allowFill'])
        self.assertIsNone(p['microgrid']['stationCount'])

    def test_save_and_profile_boundary(self):
        p=micro();updated=s.update(p['id'],copy.deepcopy(p))
        self.assertEqual(updated['types'][0]['fastPoints'],54)
        self.assertEqual(updated['cloud']['asset_template'],'电站,3,powerStation')
        bad=copy.deepcopy(updated);bad['profile']='storage'
        with self.assertRaises(s.Problem):s.update(p['id'],bad)
        bad=copy.deepcopy(updated);bad['types'][0]['fastPoints']=357
        with self.assertRaises(ValueError):s.update(p['id'],bad)
        with self.assertRaises(s.Problem):s.set_stacks(p['id'],updated['revision'],[])

    def test_allocation_is_balanced_per_type(self):
        p=s.new_project(profile='microgrid');stations=mg.station_plan(p['types'],10)
        for row in p['types']:
            counts=[x['counts'][row['type']] for x in stations]
            self.assertEqual(sum(counts),row['total'])
            self.assertLessEqual(max(counts)-min(counts),1)

    def test_microgrid_zip_actual_identity_and_periods(self):
        p=micro(True)
        plan=s.prepare(p['id'],p['revision'])
        self.assertEqual(plan['summary']['collectChannels'],8)
        self.assertTrue(all(t['realPoints']==0 for t in plan['templates']))
        self.assertEqual({t['type']:t['templatePoints'] for t in plan['templates']},
                         {kind:mg.PRESET[kind][1] for kind in mg.KINDS})
        self.assertTrue(all(t['fillPoints']==0 for t in plan['templates']))
        self.assertEqual(plan['sources'][0]['name'],'microgrid_points.json')
        job=wait_job(s.generate(p['id'],plan['planId'],'microgrid-test-key'))
        self.assertEqual(job['state'],'completed',job.get('message'))
        with zipfile.ZipFile(s.DATA/'packages'/job['id']/'gateway-projects.zip') as z:
            for name in z.namelist():
                if not name.endswith('/tb_gateway.json'):continue
                project=json.loads(z.read(name))
                for c in project['corridors_eq']:
                    kind=''.join(x for x in c['alias_name'] if not x.isdigit()).upper()
                    points=c['server']['devices'][0]['point_table'][1:]
                    self.assertEqual(len(points),mg.PRESET[kind][1])
                    self.assertEqual(sum(t['interval']==1000 for t in points),mg.PRESET[kind][2])
                    self.assertTrue(all(t['generator_type']==0 for t in points))
                    self.assertEqual(points[0]['tag'],'Tag1')
                    self.assertEqual(points[-1]['name'],f'{kind}_Tag{len(points)}')
                for c in project['corridors_zf']:
                    self.assertTrue(c['server']['client_id'].startswith('fixture-'))
        p['gateway']['allowFill']=False;s.save_project(p)
        self.assertTrue(s.prepare(p['id'],p['revision'])['planId'])
        p['types'][0]['points']+=1;s.save_project(p)
        with self.assertRaisesRegex(s.Problem,'占位补点'):s.prepare(p['id'],p['revision'])

    def test_microgrid_provision_station_rules_no_network(self):
        from app.models.entities import RunResult,EntityRecord
        from app.services.relation_rules import resolve_relation_rules
        p=micro()
        captured={}
        class Fake:
            def __init__(self,config,run_id):captured['config']=config;self.run_id=run_id
            def run(self):
                assets=[EntityRecord(entity_type='ASSET',id='station'+str(i),name='station'+str(i),notes='template=电站;templateIndex=1') for i in range(3)]
                devices=[EntityRecord(entity_type='DEVICE',id=k+str(i),name=k.lower()+str(i),notes='template='+k.lower(),mqtt_client_id='fake-'+k+str(i)) for k in mg.KINDS for i in (1,2)]
                resolution=resolve_relation_rules(captured['config'].relation_rules,assets=assets,devices=devices)
                self_case.assertEqual(len(resolution.relations),8)
                self_case.assertTrue(all(r.from_entity.entity_type=='ASSET' for r in resolution.relations))
                return RunResult(run_id=self.run_id,status='SUCCESS',devices=devices,assets=assets)
        self_case=self
        auth={k:'fixture-only' for k in ('existing_tenant_admin_username','existing_tenant_admin_password')}
        p['cloud']['base_url']='http://127.0.0.1:9999';s.save_project(p)
        with patch('app.core.orchestrator.QuickDataOrchestrator',Fake):
            job=wait_job(s.provision(p['id'],{'revision':p['revision'],'auth':auth}))
        self.assertEqual(job['state'],'completed',job.get('message'))
        self.assertEqual({b['businessType'] for b in s.get_project(p['id'])['bindings']},set(mg.KINDS))
        self.assertFalse(captured['config'].enable_telemetry)
