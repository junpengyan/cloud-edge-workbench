import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import formal_deploy as deploy


class FormalDeployTests(unittest.TestCase):
    def run_install(self, fail_start=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / 'gateway'
            config = root / 'thingsboard_gateway' / 'config'
            config.mkdir(parents=True)
            old = config / 'old'
            old.mkdir()
            old_blob = b'{"corridors_zf":[]}'
            (old / 'tb_gateway.json').write_bytes(old_blob)
            job_id = 'a' * 32
            stage = config / ('.workbench-upload-' + job_id)
            new = stage / 'new'
            new.mkdir(parents=True)
            new_blob = b'{"corridors_zf":[],"project_name":"new"}'
            (new / 'tb_gateway.json').write_bytes(new_blob)
            sha = lambda value: hashlib.sha256(value).hexdigest()
            settings = {'jobId': job_id, 'projectNames': ['new'], 'replaceProjects': ['old'],
                        'replacementHashes': {'old': sha(old_blob)},
                        'inventoryHashes': {'old': sha(old_blob)},
                        'stageHashes': {'new': sha(new_blob)}}
            calls = []

            def command(args, **kwargs):
                calls.append(args[1])
                if fail_start and args[1] == 'start' and calls.count('start') == 1:
                    raise RuntimeError('start_failed')
                return SimpleNamespace(stdout='active\n')

            with patch.object(deploy, 'CONFIG', config), patch.object(deploy, 'LOCK', root / '.workbench-validation-lock'), patch('subprocess.run', side_effect=command):
                deploy.deploy(settings)
            self.assertFalse(stage.exists())
            self.assertFalse((root / '.workbench-validation-lock').exists())
            self.assertFalse(any(root.glob('workbench-validation-*')))
            if fail_start:
                self.assertEqual((old / 'tb_gateway.json').read_bytes(), old_blob)
                self.assertFalse((config / 'new').exists())
                self.assertEqual(calls, ['stop', 'start', 'stop', 'start', 'is-active'])
            else:
                self.assertEqual((config / 'new' / 'tb_gateway.json').read_bytes(), new_blob)
                self.assertFalse(old.exists())
                self.assertEqual(calls, ['stop', 'start', 'is-active'])

    def test_success_keeps_new_project_in_config(self):
        self.run_install()

    def test_start_failure_restores_previous_project(self):
        self.run_install(fail_start=True)


if __name__ == '__main__':
    unittest.main()
