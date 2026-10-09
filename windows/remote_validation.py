"""Gateway-side round transaction with independent timeout and rollback."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

RUN = Path(__file__).resolve().parent
SETTINGS = json.loads((RUN / 'settings.json').read_text())
ROOT = Path('/root/gateway')
CONFIG = ROOT / 'thingsboard_gateway/config'
LOCK = ROOT / '.workbench-validation-lock'
state = {'status': 'preparing', 'durationSeconds': SETTINGS['durationSeconds']}
installed, moved = [], []
locked = False
started = False
legacy = 'projectNames' not in SETTINGS
names = SETTINGS.get('projectNames', [SETTINGS['projectName']])
replacements = SETTINGS.get('replaceProjects', [])
hashes = {}


def save():
    tmp = RUN / 'status.tmp'
    tmp.write_text(json.dumps(state))
    tmp.replace(RUN / 'status.json')


def service(action):
    subprocess.run(['systemctl', action, 'sprixin-gateway'], check=True, timeout=45, capture_output=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


try:
    LOCK.mkdir(mode=0o700)
    locked = True
    if not names or len(set(names)) != len(names) or len(set(replacements)) != len(replacements):
        raise RuntimeError('invalid_manifest')
    if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', n) for n in names + replacements):
        raise RuntimeError('unsafe_project_name')
    if CONFIG.resolve() != CONFIG:
        raise RuntimeError('unsafe_config_path')
    for name in names:
        target = CONFIG / name
        if target.is_symlink() or (target.exists() and name not in replacements):
            raise RuntimeError('target_exists')
    for name in replacements:
        target = CONFIG / name
        if target.is_symlink() or digest(target/'tb_gateway.json') != SETTINGS.get('replacementHashes', {}).get(name):
            raise RuntimeError('previous_config_changed')
    # Recheck client conflicts under the gateway lock immediately before changing anything.
    client_ids = set()
    for name in names:
        staged = RUN/'staged' if legacy else RUN/'staged'/name
        cfg = json.loads((staged/'tb_gateway.json').read_text())
        for channel in cfg.get('corridors_zf', []):
            client = channel['server']['client_id']
            if client in client_ids:
                raise RuntimeError('duplicate_client')
            client_ids.add(client)
    for file in CONFIG.glob('*/tb_gateway.json'):
        if file.parent.name not in replacements:
            cfg = json.loads(file.read_text())
            if any(c.get('server', {}).get('client_id') in client_ids for c in cfg.get('corridors_zf', [])):
                raise RuntimeError('client_conflict')
    if 'inventoryHashes' in SETTINGS:
        current = {f.parent.name:digest(f) for f in CONFIG.glob('*/tb_gateway.json')}
        if current != SETTINGS['inventoryHashes']:
            raise RuntimeError('inventory_changed_repeat_preflight')
    service('stop')
    (RUN/'previous-projects').mkdir(exist_ok=True)
    for name in replacements:
        (CONFIG/name).rename(RUN/'previous-projects'/name)
        moved.append(name)
    for name in names:
        target = CONFIG/name
        target.mkdir(mode=0o700)
        installed.append(name)
        shutil.copytree(RUN/'staged' if legacy else RUN/'staged'/name, target, dirs_exist_ok=True)
        hashes[name] = digest(target/'tb_gateway.json')
    service('start')
    active = subprocess.run(['systemctl','is-active','sprixin-gateway'],capture_output=True,text=True).stdout.strip()
    if active != 'active':
        raise RuntimeError('service_not_active')
    started = True
    state.update(status='running', startedAtMs=int(time.time()*1000), projectNames=names)
    state['sampleStartedAtMs'] = state['startedAtMs']+SETTINGS.get('warmupSeconds',0)*1000
    state['sampleEndsAtMs'] = state['startedAtMs']+SETTINGS['durationSeconds']*1000
    state['phase'] = 'warmup' if SETTINGS.get('warmupSeconds',0) else 'sampling'
    save()
    deadline = time.monotonic() + SETTINGS['durationSeconds']
    while time.monotonic() < deadline:
        if state['phase'] == 'warmup' and int(time.time()*1000) >= state['sampleStartedAtMs']:
            state['phase'] = 'sampling'
            save()
        if (RUN/'stop-request').exists():
            state['stoppedEarly'] = True
            break
        time.sleep(1)
except Exception as exc:
    state.update(status='failed', error=str(exc))
finally:
    state['endedAtMs'] = int(time.time()*1000)
    try:
        if installed or moved:
            state['status'] = 'restoring'
            save()
            for name in installed:
                if (CONFIG/name).is_symlink():
                    raise RuntimeError('config_changed_manual_recovery_required')
                if name in hashes and digest(CONFIG/name/'tb_gateway.json') != hashes[name]:
                    raise RuntimeError('config_changed_manual_recovery_required')
            service('stop')
            archive = RUN/'completed-projects'
            archive.mkdir(exist_ok=True)
            for name in installed:
                (CONFIG/name).rename(RUN/'completed-project' if legacy else archive/name)
            if not started:
                for name in moved:
                    (RUN/'previous-projects'/name).rename(CONFIG/name)
                state['rollbackRestored'] = True
            # Once the new round ran, old stress workloads stay archived, never restarted.
            service('start')
            state.update(status='finished' if started else 'failed', originalConfigRestored=started)
        elif locked:
            service('start')
            state['rollbackRestored'] = True
        state['serviceActive'] = subprocess.run(['systemctl','is-active','sprixin-gateway'],capture_output=True,text=True).stdout.strip()
        if state['serviceActive'] != 'active':
            raise RuntimeError('service_not_active')
    except Exception as exc:
        state.update(status='recovery_required', restoreError=str(exc))
    if locked and state.get('status') != 'recovery_required':
        LOCK.rmdir()
    save()
