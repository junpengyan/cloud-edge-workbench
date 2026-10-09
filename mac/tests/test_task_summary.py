import unittest
from datetime import datetime, timezone

from task_summary import generated_archive_summary


class TaskSummaryTests(unittest.TestCase):
    def test_generated_summary_omits_labor_savings_estimate(self):
        expected={'devices':1,'points':10,'collectChannels':1,'forwardChannels':1}
        job={'id':'a'*32,'created':datetime.now(timezone.utc).isoformat()}
        summary=generated_archive_summary('当前工程',[('test1',expected,{'project_name':'test1'})],
            job,'http://example.invalid')
        self.assertIn('## 生成概况',summary)
        self.assertNotIn('人力节省估算',summary)
        self.assertNotIn('预估人工耗时',summary)
        self.assertNotIn('预计节省时间',summary)

    def test_generated_summary_lists_device_types_per_project(self):
        expected={'counts':{'PCS':65,'BMS':48,'GROUPBMS':48,'CELL':48,'CL':1,'BOX':3},
                  'devices':213,'collectChannels':117,'forwardChannels':18,'points':86710}
        job={'id':'a'*32,'created':datetime.now(timezone.utc).isoformat()}
        summary=generated_archive_summary('当前工程',[('test1',expected,{'project_name':'test1'})],
            job,'http://example.invalid')
        self.assertIn('| 工程 | PCS | BMS | GROUPBMS | CELL | CL | BOX | 设备总数 | 采集通道 | 转发通道 | 业务点数 |',summary)
        self.assertIn('| test1 | 65 | 48 | 48 | 48 | 1 | 3 | 213 | 117 | 18 | 86,710 |',summary)


if __name__=='__main__': unittest.main()
