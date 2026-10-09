import copy
import unittest
from test_workspace import s
import capacity as c

def rows():
    return [{'type':k,'points':s.CANDIDATE[k],'total':355 if k=='PCS' else 0} for k in s.KINDS]

class CapacityTests(unittest.TestCase):
    def test_storage_capacity_uses_50_mwh_reference_and_minimizes_point_gap(self):
        points={'PCS':290,'BMS':290,'GROUPBMS':100,'CELL':900,'CL':1410,'BOX':130}
        rows=[{'type':kind,'points':points[kind],'total':0} for kind in s.KINDS]
        before=copy.deepcopy(rows)
        result=c.storage_capacity(rows,'50')
        self.assertEqual(result['totals'],{'PCS':93,'BMS':78,'GROUPBMS':78,'CELL':78,'CL':1,'BOX':8})
        self.assertEqual((result['target'],result['actual'],result['difference'],result['unitCount']),
                         (130000,130040,40,25))
        self.assertEqual(rows,before)

    def test_storage_capacity_scales_counts_and_preserves_entered_points(self):
        points={'PCS':290,'BMS':290,'GROUPBMS':100,'CELL':900,'CL':1410,'BOX':130}
        rows=[{'type':kind,'points':points[kind],'total':999} for kind in s.KINDS]
        result=c.storage_capacity(rows,'100')
        self.assertEqual(result['totals'],{'PCS':187,'BMS':157,'GROUPBMS':157,'CELL':157,'CL':1,'BOX':14})
        self.assertEqual((result['target'],result['actual'],result['difference'],result['unitCount']), (260000,259990,-10,50))
        rows[0]['points']=300
        changed=c.storage_capacity(rows,'100')
        self.assertEqual(changed['target'],result['target'])
        self.assertEqual(changed['actual'],sum(changed['totals'][kind]*rows[index]['points'] for index,kind in enumerate(s.KINDS)))

    def test_storage_capacity_rescales_reference_for_changed_point_values(self):
        rows=[{'type':kind,'points':100,'total':0} for kind in s.KINDS]
        result=c.storage_capacity(rows,'50')
        self.assertEqual((result['target'],result['actual'],result['unitCount']),(130000,130000,25))
        self.assertEqual(result['totals']['BMS'],result['totals']['GROUPBMS'])
        self.assertEqual(result['totals']['BMS'],result['totals']['CELL'])

    def test_storage_capacity_rejects_bad_capacity_and_points(self):
        rows=[{'type':kind,'points':100,'total':0} for kind in s.KINDS]
        for value in ('0','-1','NaN','Infinity','abc','10000'):
            with self.assertRaises(s.Problem):c.storage_capacity(rows,value)
        rows[0]['points']=0
        with self.assertRaises(s.Problem):c.storage_capacity(rows,'200')

    def test_storage_capacity_api_does_not_modify_project(self):
        from fastapi.testclient import TestClient
        import server
        p=s.new_project('capacity-reference','storage');s.save_project(p)
        values=[{'type':kind,'points':100,'total':0} for kind in s.KINDS]
        response=TestClient(server.app).post('/api/projects/'+p['id']+'/capacity',
            headers={'X-Workspace-Token':server.TOKEN},
            json={'revision':p['revision'],'types':values,'capacityMwh':'100'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['difference'],0)
        self.assertEqual(response.json()['unitCount'],50)
        self.assertEqual(s.get_project(p['id']),p)

    def test_units_and_invalid_inputs(self):
        for text in ['100000','10万','10w','10W点',' 10 万点 ','0.1万']:
            self.assertEqual(c.target_points(text),1000 if text=='0.1万' else 100000)
        for text in ['0','-1','NaN','10.1','2001万','abc',None]:
            with self.assertRaises(s.Problem):c.target_points(text)

    def test_pcs_100k_rounds_up_without_mutating_rows(self):
        r=rows();before=copy.deepcopy(r);out=c.calculate('storage',r,'10w','PCS')
        self.assertEqual((out['totals']['PCS'],out['actual'],out['difference']),(345,100050,50))
        self.assertEqual(r,before)

    def test_bms_group_is_kept_together(self):
        out=c.calculate('storage',rows(),'10万','BMS')
        self.assertEqual([out['totals'][k] for k in ('BMS','GROUPBMS','CELL')],[78,78,78])
        self.assertEqual(out['actual'],100620)
        self.assertEqual(out['totals']['PCS'],0)

    def test_microgrid_ratio_is_small_and_above_target(self):
        from microgrid import PRESET
        r=[{'type':k,'total':v[0],'points':v[1]} for k,v in PRESET.items()]
        out=c.calculate('microgrid',r,'10万','ratio')
        self.assertGreaterEqual(out['actual'],100000)
        self.assertLess(out['actual'],100000+sum(x['points'] for x in r))
        self.assertLess(out['devices'],100)

    def test_storage_ratio_fills_all_types_from_empty_project(self):
        r=[{'type':k,'points':s.CANDIDATE[k],'total':0} for k in s.KINDS]
        before=copy.deepcopy(r)
        out=c.calculate('storage',r,'10万','ratio')
        self.assertGreaterEqual(out['actual'],100000)
        self.assertTrue(all(out['totals'][k]>0 for k in s.KINDS))
        self.assertEqual(len({out['totals'][k] for k in ('BMS','GROUPBMS','CELL')}),1)
        self.assertEqual(r,before)

    def test_missing_points_ratio_and_limits(self):
        r=rows();r[0]['points']=None
        with self.assertRaises(s.Problem):c.calculate('storage',r,'10万','PCS')
        r=rows();r[1]['total']=1
        with self.assertRaises(s.Problem):c.calculate('storage',r,'10万','ratio')
        with self.assertRaises(s.Problem):c.calculate('storage',rows(),'2000万','PCS')

    def test_bound_project_capacity_rejects_exceeding_pool(self):
        from fastapi.testclient import TestClient
        import server
        from test_workspace import ready
        p=ready();client=TestClient(server.app)
        resp=client.post('/api/projects/'+p['id']+'/capacity',headers={'X-Workspace-Token':server.TOKEN},json={'revision':p['revision'],'types':rows(),'target':'10万','mode':'PCS'})
        self.assertEqual(resp.status_code,422)
        self.assertEqual(s.get_project(p['id']),p)

    def test_bound_project_capacity_can_select_subset_without_mutation(self):
        from fastapi.testclient import TestClient
        import server
        from test_workspace import ready
        p = ready()
        client = TestClient(server.app)
        response = client.post('/api/projects/'+p['id']+'/capacity',
            headers={'X-Workspace-Token':server.TOKEN},
            json={'revision':p['revision'], 'types':p['types'], 'target':'290', 'mode':'PCS'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['totals']['PCS'], 1)
        self.assertEqual(response.json()['devices'], 1)
        self.assertEqual(s.get_project(p['id']), p)
