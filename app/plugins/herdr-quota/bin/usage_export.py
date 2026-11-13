"""Publish normalized quota data, never collector credentials or raw errors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

_LIB = Path(os.environ.get("HERDR_APP_PLUGINS_ROOT",
                           str(Path(__file__).resolve().parents[3]))) / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import herdr_app


def _config_root() -> Path:
    override = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(override) if override else herdr_app.config_dir("herdr.quota")


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.usage-')
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(payload, output, ensure_ascii=False)
            output.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def snapshot(providers):
    return {
        'schema_version': 1, 'generated_at': time.time(),
        'source': 'central-herdr-quota', 'max_age_seconds': 900,
        'providers': [
            {'name': p.name, 'status': p.status, 'plan': p.plan,
             'updated_at': p.updated_at,
             'details': p.details,
             'windows': [
                 {'label': w.label, 'used_percent': w.used_percent,
                  'remaining_percent': max(0, 100 - w.used_percent),
                  'resets_at': w.resets_at, 'reset_text': w.reset_text,
                  'duration_minutes': w.duration_minutes}
                 for w in p.windows]}
            for p in providers]}


REMOTE_WRITE = '''import json, os, sys, tempfile
from pathlib import Path
p = Path.home() / '.local/state/llm-usage/snapshot.json'
data = json.load(sys.stdin)
assert data['schema_version'] == 1
p.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.usage-')
try:
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
        f.write('\\n')
    os.replace(tmp, p)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
'''


def publish(providers):
    config = _config_root()
    payload = snapshot(providers)
    atomic_json(config / 'usage.json', payload)
    targets_file = config / 'export-targets.json'
    targets = json.loads(targets_file.read_text()) if targets_file.exists() else []
    results = []
    for target in targets:
        try:
            result = subprocess.run(
                ['docker', 'exec', '-i', '-u', target['user'], target['container'],
                 'python3', '-c', REMOTE_WRITE],
                input=json.dumps(payload), text=True, capture_output=True, timeout=10)
            status = 'ok' if result.returncode == 0 else 'delivery_failed'
        except (OSError, subprocess.TimeoutExpired):
            status = 'delivery_failed'
        results.append({'container': target['container'], 'status': status})
    atomic_json(config / 'export-status.json',
                {'attempted_at': time.time(), 'targets': results})


def record_failure():
    config = _config_root()
    try:
        atomic_json(config / 'export-status.json',
                    {'attempted_at': time.time(), 'status': 'export_failed'})
    except OSError:
        # Consumers still reject the previous snapshot once it ages out.
        pass
