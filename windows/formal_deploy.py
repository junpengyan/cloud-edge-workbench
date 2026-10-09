"""One-shot, persistent gateway install. SETTINGS is supplied by the caller."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path('/root/gateway')
CONFIG = ROOT / 'thingsboard_gateway/config'
LOCK = ROOT / '.workbench-validation-lock'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service(action):
    subprocess.run(['systemctl', action, 'sprixin-gateway'], check=True, timeout=45, capture_output=True)


def deploy(settings):
    names = settings['projectNames']
    replacements = settings['replaceProjects']
    stage = CONFIG / ('.workbench-upload-' + settings['jobId'])
    previous = stage / 'previous'
    moved, installed = [], []
    locked = stopped = False
    state = {'status': 'preparing'}
    try:
        LOCK.mkdir(mode=0o700)
        locked = True
        if CONFIG.resolve() != CONFIG or stage.is_symlink() or not stage.is_dir():
            raise RuntimeError('unsafe_config_path')
        if not re.fullmatch(r'[a-f0-9]{32}', settings['jobId']):
            raise RuntimeError('unsafe_job_id')
        if not names or len(set(names)) != len(names) or len(set(replacements)) != len(replacements):
            raise RuntimeError('invalid_manifest')
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', name) for name in names + replacements):
            raise RuntimeError('unsafe_project_name')
        if previous.exists():
            raise RuntimeError('staging_conflict')
        for name in names:
            target = CONFIG / name
            source_dir = stage / name
            source = source_dir / 'tb_gateway.json'
            if target.is_symlink() or (target.exists() and name not in replacements):
                raise RuntimeError('target_exists')
            if source_dir.is_symlink() or not source_dir.is_dir() or source.is_symlink() or not source.is_file() or digest(source) != settings['stageHashes'][name]:
                raise RuntimeError('staged_config_changed')
        for name in replacements:
            target = CONFIG / name
            if target.is_symlink() or not (target / 'tb_gateway.json').is_file():
                raise RuntimeError('previous_config_missing')
            if digest(target / 'tb_gateway.json') != settings['replacementHashes'][name]:
                raise RuntimeError('previous_config_changed')
        current = {}
        for path in CONFIG.glob('*/tb_gateway.json'):
            if path.is_symlink() or path.parent.is_symlink():
                raise RuntimeError('unsafe_existing_config')
            current[path.parent.name] = digest(path)
        if current != settings['inventoryHashes']:
            raise RuntimeError('inventory_changed_repeat_preflight')
        client_ids = set()
        for name in names:
            cfg = json.loads((stage / name / 'tb_gateway.json').read_text())
            for channel in cfg.get('corridors_zf', []):
                client_id = channel['server']['client_id']
                if client_id in client_ids:
                    raise RuntimeError('duplicate_client')
                client_ids.add(client_id)
        for path in CONFIG.glob('*/tb_gateway.json'):
            if path.parent.name in replacements:
                continue
            cfg = json.loads(path.read_text())
            if any(channel.get('server', {}).get('client_id') in client_ids for channel in cfg.get('corridors_zf', [])):
                raise RuntimeError('client_conflict')
        service('stop')
        stopped = True
        previous.mkdir(mode=0o700)
        for name in replacements:
            (CONFIG / name).rename(previous / name)
            moved.append(name)
        for name in names:
            if (stage / name).is_symlink() or digest(stage / name / 'tb_gateway.json') != settings['stageHashes'][name]:
                raise RuntimeError('staged_config_changed')
            (stage / name).rename(CONFIG / name)
            installed.append(name)
        service('start')
        if subprocess.run(['systemctl', 'is-active', 'sprixin-gateway'], capture_output=True, text=True).stdout.strip() != 'active':
            raise RuntimeError('service_not_active')
        state = {'status': 'completed', 'startedAtMs': int(time.time() * 1000),
                 'projectNames': names, 'serviceActive': 'active'}
    except Exception as exc:
        state = {'status': 'failed', 'error': str(exc)}
        if stopped:
            try:
                service('stop')
                for name in installed:
                    target = CONFIG / name
                    if target.is_symlink() or digest(target / 'tb_gateway.json') != settings['stageHashes'][name]:
                        raise RuntimeError('config_changed_manual_recovery_required')
                    target.rename(stage / name)
                for name in moved:
                    (previous / name).rename(CONFIG / name)
                service('start')
                if subprocess.run(['systemctl', 'is-active', 'sprixin-gateway'], capture_output=True, text=True).stdout.strip() != 'active':
                    raise RuntimeError('service_not_active')
                state['rollbackRestored'] = True
                state['serviceActive'] = 'active'
            except Exception as restore_error:
                state.update(status='recovery_required', restoreError=str(restore_error))
    finally:
        if state['status'] != 'recovery_required':
            try:
                if stage.exists():
                    shutil.rmtree(stage)
                if locked:
                    LOCK.rmdir()
            except Exception as cleanup_error:
                state.update(status='recovery_required', cleanupError=str(cleanup_error))
        print(json.dumps(state))


if __name__ == '__main__':
    deploy(SETTINGS)
