"""Decrypt and install a cloud-edge-workbench migration bundle on macOS."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BUNDLE_MAGIC = b'CEWBUNDLE1'


def decrypt_bundle(source: Path, key: bytes, destination: Path):
    with source.open('rb') as encrypted:
        header = encrypted.read(len(BUNDLE_MAGIC) + 12)
        if not header.startswith(BUNDLE_MAGIC) or len(header) != len(BUNDLE_MAGIC) + 12:
            raise ValueError('迁移包格式无效')
        nonce = header[len(BUNDLE_MAGIC):]
        encrypted.seek(-16, os.SEEK_END)
        tag = encrypted.read(16)
        remaining = encrypted.tell() - 16 - len(header)
        encrypted.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        with destination.open('wb') as output:
            while remaining:
                chunk = encrypted.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('迁移包不完整')
                output.write(decryptor.update(chunk))
                remaining -= len(chunk)
            output.write(decryptor.finalize())


def extract_bundle(source: Path, target: Path):
    with zipfile.ZipFile(source) as archive:
        for item in archive.infolist():
            relative = Path(item.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in item.filename:
                raise ValueError('迁移包包含不安全路径')
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as incoming, destination.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)


def rebase_current_template_paths(target: Path):
    projects = target / 'data' / 'projects'
    updated = 0
    for path in projects.glob('*.json'):
        project = json.loads(path.read_text(encoding='utf-8'))
        gateway = project.get('gateway', {})
        changes = {}
        for field in ('templateDbPath', 'templateBmsCsvPath'):
            original = str(gateway.get(field) or '')
            if not original or Path(original).is_file():
                continue
            name = PureWindowsPath(original).name
            replacement = target / 'templates' / name
            if replacement.is_file():
                changes[field] = str(replacement)
        if not changes:
            continue
        project['gateway'] = {**gateway, **changes}
        project['revision'] += 1
        project['updated'] = datetime.now(timezone.utc).isoformat()
        snapshot = projects / project['id'] / f"v{project['revision']}.json"
        if snapshot.exists():
            raise ValueError(f'目标工程版本冲突：{project["id"]}')
        content = json.dumps(project, ensure_ascii=False, indent=2)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(content, encoding='utf-8')
        path.write_text(content, encoding='utf-8')
        updated += 1
    return updated


def install(bundle: Path, key_path: Path, target: Path):
    if target.exists():
        raise ValueError('目标目录已存在，请选择一个新的空目录，避免覆盖现有数据')
    key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError('迁移密钥长度无效')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cew-install-', dir=target.parent) as temporary:
        archive = Path(temporary) / 'bundle.zip'
        try:
            decrypt_bundle(bundle, key, archive)
        except Exception as exc:
            raise ValueError('迁移包解密失败，请核对包与密钥是否配套') from exc
        staging = Path(temporary) / 'workspace'
        staging.mkdir()
        extract_bundle(archive, staging)
        destination_key = staging / 'data' / 'portable-key.bin'
        destination_key.parent.mkdir(parents=True, exist_ok=True)
        destination_key.write_bytes(key)
        destination_key.chmod(0o600)
        updated = rebase_current_template_paths(staging)
        staging.rename(target)
    print(f'已安装到 {target}；更新 {updated} 个工程的当前模板路径。')
    print(f'下一步：cd "{target}" && ./install-macos.sh && ./start-macos.sh')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('key', type=Path)
    parser.add_argument('target', type=Path, nargs='?', default=Path.home() / 'cloud-edge-workbench')
    args = parser.parse_args()
    if sys.platform != 'darwin' and os.environ.get('CEW_TEST_INSTALL') != '1':
        parser.error('此安装脚本应在 macOS 上运行')
    install(args.bundle.resolve(), args.key.resolve(), args.target.expanduser().resolve())


if __name__ == '__main__':
    main()
