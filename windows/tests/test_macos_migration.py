"""Offline checks for the encrypted Mac migration container."""
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import workspace as s
from scripts.create_macos_bundle import EncryptedWriter, protected_file
from scripts.install_macos_bundle import decrypt_bundle, extract_bundle, rebase_current_template_paths


class MacMigrationTests(unittest.TestCase):
    def test_encrypted_bundle_round_trip_and_key_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            key=os.urandom(32)
            bundle=folder/'bundle.cew'
            writer=EncryptedWriter(bundle,key)
            with zipfile.ZipFile(writer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('data/example.txt','迁移数据')
            writer.close()
            archive_path=folder/'bundle.zip'
            decrypt_bundle(bundle,key,archive_path)
            target=folder/'extracted';extract_bundle(archive_path,target)
            self.assertEqual((target/'data/example.txt').read_text(encoding='utf-8'),'迁移数据')
            with self.assertRaises(Exception):
                decrypt_bundle(bundle,os.urandom(32),folder/'wrong.zip')

    def test_portable_secret_and_template_rebase(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);key=os.urandom(32);nonce=os.urandom(12)
            key_path=folder/'portable-key.bin';key_path.write_bytes(key)
            blob=s.PORTABLE_SECRET_MAGIC+nonce+AESGCM(key).encrypt(nonce,b'fixture',None)
            with patch.dict(os.environ,{'UNIFIED_PORTABLE_KEY_FILE':str(key_path)}):
                self.assertEqual(s.crypt(blob,True),b'fixture')
            self.assertTrue(protected_file(Path('data/vault/fixture.bin')))
            self.assertTrue(protected_file(Path('data/deployments/fixture/config.dpapi')))
            self.assertFalse(protected_file(Path('data/packages/fixture/gateway.zip')))
            (folder/'templates').mkdir();(folder/'templates/db.xml').write_text('fixture')
            projects=folder/'data/projects';projects.mkdir(parents=True)
            value={'id':'a'*32,'revision':1,'gateway':{'templateDbPath':'E:\\old\\db.xml'}}
            import json
            (projects/('a'*32+'.json')).write_text(json.dumps(value),encoding='utf-8')
            self.assertEqual(rebase_current_template_paths(folder),1)
            updated=json.loads((projects/('a'*32+'.json')).read_text(encoding='utf-8'))
            self.assertEqual(updated['revision'],2)
            self.assertEqual(updated['gateway']['templateDbPath'],str(folder/'templates/db.xml'))
            self.assertTrue((projects/('a'*32)/'v2.json').is_file())


if __name__=='__main__':
    unittest.main()
