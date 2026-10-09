"""Offline coverage for repeatedly generating from one retained device pool."""
import copy
import json
import unittest
import zipfile
from unittest.mock import patch
from test_workspace import fixture, wait_job, s


class DevicePoolTests(unittest.TestCase):
    def pool_project(self):
        doc = fixture()
        original = copy.deepcopy(doc)
        doc['devices'] = []
        doc['relations'] = []
        for index in range(1, 3):
            for device in original['devices']:
                item = copy.deepcopy(device)
                for key in ('id', 'name', 'mqtt_client_id', 'mqtt_username'):
                    item[key] += str(index)
                doc['devices'].append(item)
            for relation in original['relations']:
                item = copy.deepcopy(relation)
                item['from_id'] += str(index)
                item['to_id'] += str(index)
                doc['relations'].append(item)
        p = s.import_doc(doc, 'run-pool', 'pool-fixture')
        for row in p['types']:
            row['points'] = 10
        p['pointsConfirmed'] = True
        return s.update(p['id'], p)

    def test_reduce_change_points_and_restore_without_recreating_devices(self):
        p = self.pool_project()
        pool = copy.deepcopy(p['bindings'])
        packages = []
        with patch('app.platform.thingsboard_client.ThingsBoardClient', side_effect=AssertionError('No platform access')):
            for index, (count, points) in enumerate([(2, 10), (1, 20), (2, 15)]):
                for row in p['types']:
                    row.update(total=count, points=points)
                p = s.update(p['id'], p)
                self.assertEqual(p['bindings'], pool)
                plan = s.prepare(p['id'], p['revision'])
                self.assertEqual(plan['summary']['totalDevices'], count * 6)
                self.assertEqual(plan['summary']['totalPoints'], count * points * 6)
                job = wait_job(s.generate(p['id'], plan['planId'], 'pool-generation-'+str(index)))
                self.assertEqual(job['state'], 'completed', job['message'])
                path = s.DATA/'packages'/job['id']/'gateway-projects.zip'
                packages.append((path, path.read_bytes()))
                aliases = set()
                with zipfile.ZipFile(path) as archive:
                    for expected in plan['projects']:
                        name = expected['name']
                        summary = archive.read('summary.md').decode('utf-8')
                        self.assertIn('工程文件已生成并校验', summary)
                        self.assertIn(f"- 逻辑设备：{expected['devices']}", summary)
                        self.assertIn(f"- 业务测点：{expected['points']}", summary)
                        self.assertIn('平台上报：未在线验证', summary)
                        self.assertNotIn('fake-test-secret', summary)
                    for name in archive.namelist():
                        if name.endswith('/tb_gateway.json'):
                            config = json.loads(archive.read(name))
                            s.verify_bindings(config, p, aliases)
                self.assertEqual(aliases, {b['collectAlias'] for b in s.active_bindings(p)})
            for path, original in packages:
                self.assertEqual(path.read_bytes(), original)

    def test_reject_over_capacity_and_partial_bms_stack(self):
        p = self.pool_project()
        for totals in [{'PCS':3}, {'BMS':1}]:
            changed = copy.deepcopy(p)
            for row in changed['types']:
                row['total'] = totals.get(row['type'], row['total'])
            with self.assertRaises(s.Problem):
                s.update(p['id'], changed)
            self.assertEqual(s.get_project(p['id']), p)

    def test_zero_count_excludes_devices_without_discarding_pool(self):
        p = self.pool_project()
        for row in p['types']:
            if row['type'] in ('BMS','GROUPBMS','CELL'):
                row['total'] = 0
        p = s.update(p['id'], p)
        self.assertEqual(len(p['bindings']), 12)
        self.assertEqual({b['businessType'] for b in s.active_bindings(p)}, {'PCS','CL','BOX'})

    def test_configuration_check_before_binding_has_no_platform_calls(self):
        from fastapi.testclient import TestClient
        import server
        p = self.pool_project()
        p['bindings'] = []
        p['sourceStatus'] = None
        p['mappingIssues'] = ['组合关系待确认']
        client = TestClient(server.app)
        with patch.object(s, 'get_project', return_value=p), patch('app.platform.thingsboard_client.ThingsBoardClient', side_effect=AssertionError('No platform calls')):
            response = client.post('/api/projects/'+p['id']+'/configuration-check',
                headers={'X-Workspace-Token':server.TOKEN}, json={'revision':p['revision']})
            stale = client.post('/api/projects/'+p['id']+'/configuration-check',
                headers={'X-Workspace-Token':server.TOKEN}, json={'revision':p['revision']-1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {'revision','summary','templates','sharedDevices'})
        self.assertEqual(response.json()['summary']['totalDevices'], 12)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(p['mappingIssues'], ['组合关系待确认'])

    def test_microgrid_selects_independent_types(self):
        p = {'profile':'microgrid', 'types':[{'type':'PCS','total':1},{'type':'ESS','total':0}],
             'bindings':[{'businessType':'PCS','collectAlias':'pcs1'},
                         {'businessType':'PCS','collectAlias':'pcs2'},
                         {'businessType':'ESS','collectAlias':'ess1'}]}
        self.assertEqual(s.active_bindings(p), [p['bindings'][0]])


if __name__ == '__main__':
    unittest.main()
