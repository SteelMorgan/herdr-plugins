#!/usr/bin/python3
"""Bounded startup retry. Reuse designated panels; never type into a busy pane."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

ROOT = Path.home() / 'projects/herdr-plugins/plugins'
STATE = Path.home() / '.local/state/herdr-dashboards'
HERDR = os.environ.get('HERDR_BIN', str(Path.home() / '.local/bin/herdr'))
PANELS = {
    'LLM Usage': ('herdr-quota', 'herdr.quota'),
    'Container resources': ('herdr-resources', 'herdr.resources'),
    'Dev server health': ('herdr-pve-health', 'herdr.pve-health'),
}


def api(*args):
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=5)
    if r.returncode:
        raise RuntimeError('Herdr API unavailable')
    if args[:2] == ('pane', 'run') and not r.stdout.strip():
        return {}
    return json.loads(r.stdout)['result']


def log(event, **fields):
    with (STATE / 'startup.log').open('a') as f:
        f.write(json.dumps({'time': time.time(), 'event': event, **fields}) + '\n')


def classify(info, dashboard):
    processes = info.get('foreground_processes', [])
    if any(dashboard in p.get('argv', []) for p in processes):
        return 'running'
    if len(processes) == 1:
        p = processes[0]
        if p.get('pid') == info.get('shell_pid') and p.get('name') in ('bash', 'sh', 'zsh', 'fish'):
            return 'shell'
    return 'busy'


def restore_once():
    workspaces = api('workspace', 'list')['workspaces']
    explicit = os.environ.get('HERDR_DASHBOARDS_WORKSPACE')
    matches = [w for w in workspaces if (w['workspace_id'] == explicit if explicit else w.get('label') == 'dashboards')]
    if len(matches) != 1:
        return False
    workspace = matches[0]['workspace_id']
    panes = api('pane', 'list', '--workspace', workspace)['panes']
    complete = True
    for label, (directory, plugin) in PANELS.items():
        candidates = [p for p in panes if p.get('label') == label]
        if len(candidates) != 1:
            complete = False
            continue
        pane = candidates[0]
        dashboard = str(ROOT / directory / 'bin/dashboard.py')
        info = api('pane', 'process-info', '--pane', pane['pane_id'])['process_info']
        state = classify(info, dashboard)
        if state == 'running':
            continue
        if state == 'busy':
            log('skip_busy', pane=pane['pane_id'], label=label)
            continue
        # Validate cwd as well as label before injecting a command.
        if pane.get('foreground_cwd', pane.get('cwd')) != str(ROOT / directory):
            log('skip_wrong_cwd', pane=pane['pane_id'], label=label)
            continue
        # Recheck after discovery so another process that started is respected.
        if classify(api('pane', 'process-info', '--pane', pane['pane_id'])['process_info'], dashboard) != 'shell':
            continue
        config = str(Path.home() / '.config/herdr/plugins/config' / plugin)
        command = 'HERDR_PLUGIN_CONFIG_DIR=' + shlex.quote(config) + ' /usr/bin/python3 ' + shlex.quote(dashboard)
        api('pane', 'run', pane['pane_id'], command)
        log('launched', pane=pane['pane_id'], label=label)
        complete = False
    return complete


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'startup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        deadline = time.monotonic() + 60
        log('start')
        while time.monotonic() < deadline:
            try:
                if restore_once():
                    log('complete')
                    return
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as e:
                log('retry', reason=type(e).__name__)
            time.sleep(2)
        log('timeout')


if __name__ == '__main__':
    main()
