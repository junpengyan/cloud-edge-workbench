"""Export a Windows workspace as an encrypted, portable macOS migration bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workspace

BUNDLE_MAGIC = b'CEWBUNDLE1'
SKIP_DIRS = {'.git', '.venv', '.validation-deps', '__pycache__', 'dist', '.playwright-cli'}
SKIP_DATA_DIRS = {'test-temp'}
SKIP_DATA_FILES = {'server.log', 'server.err.log', 'server.pid', 'portable-key.bin'}


class EncryptedWriter:
    def __init__(self, path: Path, key: bytes):
        self.file = path.open('wb')
        nonce = os.urandom(12)
        self.file.write(BUNDLE_MAGIC + nonce)
        self.encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()

    def write(self, data: bytes) -> int:
        self.file.write(self.encryptor.update(data))
        return len(data)

    def flush(self) -> None:
        self.file.flush()

    def close(self) -> None:
        self.file.write(self.encryptor.finalize())
        self.file.write(self.encryptor.tag)
        self.file.close()


def files_to_export():
    for base, dirs, files in os.walk(ROOT):
        folder = Path(base)
        relative = folder.relative_to(ROOT)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and
                         not (relative == Path('data') and d in SKIP_DATA_DIRS) and
                         not (folder / d).is_symlink())
        for name in sorted(files):
            path = folder / name
            if path.is_symlink() or name.endswith(('.tmp', '.partial')):
                continue
            if relative.parts and relative.parts[0] == 'data' and name in SKIP_DATA_FILES:
                continue
            yield path


def protected_file(relative: Path) -> bool:
    return relative.parts[0] == 'data' and (relative.suffix == '.dpapi' or
            (len(relative.parts) >= 3 and relative.parts[1] == 'vault' and relative.suffix == '.bin'))


def write_portable_secret(archive: zipfile.ZipFile, relative: Path, path: Path, key: bytes):
    plaintext = workspace.crypt(path.read_bytes(), True)
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with archive.open(str(relative).replace('\\', '/'), 'w', force_zip64=True) as output:
        output.write(workspace.PORTABLE_SECRET_MAGIC + nonce)
        view = memoryview(plaintext)
        for offset in range(0, len(view), 1024 * 1024):
            output.write(encryptor.update(view[offset:offset + 1024 * 1024]))
        output.write(encryptor.finalize())
        output.write(encryptor.tag)
    del plaintext


def inventory(paths):
    total = sum(path.stat().st_size for path in paths)
    protected = [path for path in paths if protected_file(path.relative_to(ROOT))]
    print(f'{len(paths)} files, {total / 1024**3:.2f} GiB; {len(protected)} encrypted credential files')
    return total, protected


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def export(output: Path, paths):
    output.mkdir(parents=True, exist_ok=False)
    key = os.urandom(32)
    key_path = output / 'migration-key.bin'
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(key)
    bundle = output / 'cloud-edge-workbench-macos.cew'
    writer = EncryptedWriter(bundle, key)
    try:
        with zipfile.ZipFile(writer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1,
                             allowZip64=True) as archive:
            for index, path in enumerate(paths, 1):
                relative = path.relative_to(ROOT)
                if protected_file(relative):
                    write_portable_secret(archive, relative, path, key)
                else:
                    archive.write(path, str(relative).replace('\\', '/'))
                if index % 100 == 0 or index == len(paths):
                    print(f'Packed {index}/{len(paths)} files', flush=True)
        writer.close()
    except Exception:
        writer.file.close()
        bundle.unlink(missing_ok=True)
        key_path.unlink(missing_ok=True)
        raise
    shutil.copy2(ROOT / 'scripts' / 'install_macos_bundle.py', output / 'install_macos_bundle.py')
    shutil.copy2(ROOT / 'scripts' / 'install_macos_bundle.sh', output / 'install_macos_bundle.sh')
    shutil.copy2(ROOT / 'scripts' / 'MACOS_MIGRATION.md', output / 'README.md')
    checksums = {path.name: file_digest(path)
                 for path in (bundle, output / 'install_macos_bundle.py', output / 'install_macos_bundle.sh')}
    (output / 'SHA256SUMS.json').write_text(json.dumps(checksums, indent=2) + '\n', encoding='utf-8')
    print(f'Bundle: {bundle} ({bundle.stat().st_size / 1024**3:.2f} GiB)')
    print(f'Separate key: {key_path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', action='store_true', help='only count files and sizes')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / ('mac-migration-' + datetime.now().strftime('%Y%m%d-%H%M%S')))
    args = parser.parse_args()
    paths = list(files_to_export())
    _, protected = inventory(paths)
    if args.inventory:
        for path in sorted(protected, key=lambda item: item.stat().st_size, reverse=True)[:5]:
            print(f'{path.relative_to(ROOT)}: {path.stat().st_size / 1024**2:.0f} MiB')
        return
    if os.name != 'nt':
        parser.error('DPAPI export must run under the original Windows account')
    export(args.output.resolve(), paths)


if __name__ == '__main__':
    main()
