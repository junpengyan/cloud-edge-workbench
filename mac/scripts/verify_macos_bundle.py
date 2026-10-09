"""Verify the encrypted migration bundle, archive CRCs, and one portable credential."""
import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from install_macos_bundle import decrypt_bundle


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024),b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    folder=args.directory.resolve()
    bundle=folder/'cloud-edge-workbench-macos.cew'
    key=(folder/'migration-key.bin').read_bytes()
    expected=json.loads((folder/'SHA256SUMS.json').read_text(encoding='utf-8'))
    if digest(bundle)!=expected[bundle.name]:
        raise ValueError('迁移包 SHA256 不一致')
    with tempfile.TemporaryDirectory(prefix='cew-verify-',dir=folder.parent) as temporary:
        archive_path=Path(temporary)/'bundle.zip'
        decrypt_bundle(bundle,key,archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            bad=archive.testzip()
            if bad: raise ValueError('迁移包 ZIP 校验失败：'+bad)
            files=archive.namelist()
            secrets=[name for name in files if name.startswith('data/vault/') and name.endswith('.bin')]
            if not secrets: raise ValueError('迁移包缺少凭据文件')
            blob=archive.read(secrets[0])
            if not blob.startswith(b'CEW1'):
                raise ValueError('凭据文件尚未转换为可移植格式')
            AESGCM(key).decrypt(blob[4:16],blob[16:],None)
            print(f'验证通过：{len(files)} 个文件，凭据已转换，ZIP CRC 与 SHA256 均正确')


if __name__=='__main__':
    main()
