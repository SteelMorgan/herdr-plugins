#!/usr/bin/python3
"""Autostart PROD SSH sidecar and reap confirmed stale server SSH listeners."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

DIRECTORY = Path('/home/steel/.local/state/onec-v8sm-watchdog')
SCRIPT = '/usr/local/libexec/onec-v8sm-watchdog.py'
PARENT = '1c-ai-sandbox'
TUNNEL = 'onec-v8sm-tunnel'
INTERVAL = 20
DOCKER = '/usr/bin/docker'
SSH_CONFIG = '/home/steel/.local/state/herdr-mirror/.mirror-pane/proxmox/bootstrap/ssh_config'
previous = None


def state(value, **details):
    global previous
    data = {'state': value, 'checked_at': datetime.datetime.now().astimezone().isoformat(), **details}
    temp = DIRECTORY / 'status.tmp'
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, DIRECTORY / 'status.json')
    stamp = (value, json.dumps(details, sort_keys=True))
    if stamp != previous:
        log = DIRECTORY / 'watchdog.log'
        if log.exists() and log.stat().st_size > 1024 * 1024:
            os.replace(log, DIRECTORY / 'watchdog.log.1')
        with log.open('a') as out:
            out.write(json.dumps(data) + '\n')
        previous = stamp
    return data


def docker(*args):
    return subprocess.run([DOCKER, *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=15)


def inspect(name):
    template = '{"id":{{json .Id}},"state":{{json .State.Status}},"started":{{json .State.StartedAt}},"network":{{json .HostConfig.NetworkMode}}}'
    result = docker('inspect', '--format', template, name)
    return json.loads(result.stdout) if result.returncode == 0 else None


def ssh_guard(tunnel_started=None):
    """Independent public jump route; failure must never trigger a local restart.

    tunnel_started (container StartedAt) lets the guard prove idle orphans:
    a listener session older than the current container start is stale."""
    try:
        remote = 'sudo -n /usr/local/sbin/onec-ssh-tunnel-guard check'
        if tunnel_started:
            remote += ' --container-started ' + shlex.quote(tunnel_started)
        result = subprocess.run(
            ['/usr/bin/ssh', '-F', SSH_CONFIG, '-o', 'ControlMaster=no',
             '-o', 'ControlPath=none', 'gbig-1c-app', remote],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=25)
        if result.returncode:
            return {'state': 'inspection_unavailable_no_action'}
        data = json.loads(result.stdout)
        return {'state': data.get('guard', {}).get('state', data.get('state')),
                'listener_pid': data.get('identity', {}).get('pid')}
    except (subprocess.SubprocessError, OSError, ValueError):
        return {'state': 'inspection_unavailable_no_action'}


def check():
    if (DIRECTORY / 'paused').exists():
        return state('paused')
    parent = inspect(PARENT)
    if parent is None:
        return state('waiting_for_docker_or_parent')
    if parent['state'] != 'running':
        return state('waiting_for_parent', parent_state=parent['state'])
    target = inspect(TUNNEL)
    if target is None:
        return state('missing_tunnel', message='Existing tunnel is missing; not recreating containers')
    peer = target['network'].removeprefix('container:')
    if not target['network'].startswith('container:') or not (peer == PARENT or parent['id'].startswith(peer)):
        return state('parent_identity_changed', message='Tunnel belongs to another container namespace; manual rebind required')
    guard = ssh_guard(target.get('started'))
    if target['state'] == 'running':
        parent_started = datetime.datetime.fromisoformat(parent['started'].replace('Z', '+00:00'))
        tunnel_started = datetime.datetime.fromisoformat(target['started'].replace('Z', '+00:00'))
        if tunnel_started < parent_started:
            # A live SSH process may retain the old parent's network namespace.
            # Rejoin only this sidecar; never restart the sandbox or 1C.
            result = docker('restart', '--time', '5', TUNNEL)
            if result.returncode:
                return state('rebind_failed', message=result.stderr.strip()[-400:])
            return state('rebound_after_parent_restart', parent_id=parent['id'], tunnel_id=target['id'])
        return state('running', parent_id=parent['id'], tunnel_id=target['id'], ssh_guard=guard)
    if target['state'] not in ('exited', 'created'):
        return state('waiting_for_tunnel', tunnel_state=target['state'], ssh_guard=guard)
    result = docker('start', TUNNEL)
    if result.returncode:
        return state('start_failed', message=result.stderr.strip()[-400:])
    return state('started', parent_id=parent['id'], tunnel_id=target['id'])


def live_pid():
    try:
        pid = int((DIRECTORY / 'pid').read_text())
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if SCRIPT.encode() in argv and b'daemon' in argv:
            os.kill(pid, 0)
            return pid
    except (OSError, ValueError):
        pass
    return None


def main():
    os.umask(0o077)
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    action = sys.argv[1] if len(sys.argv) == 2 else 'status'
    if action == 'pause':
        (DIRECTORY / 'paused').touch()
        print('Autostart paused; running tunnel is left untouched')
        return
    if action == 'resume':
        (DIRECTORY / 'paused').unlink(missing_ok=True)
        action = 'start'
    if action == 'status':
        data = json.loads((DIRECTORY / 'status.json').read_text()) if (DIRECTORY / 'status.json').exists() else {}
        data.update(daemon_pid=live_pid(), paused=(DIRECTORY / 'paused').exists())
        print(json.dumps(data, indent=2))
        return
    if action in ('stop', 'restart'):
        pid = live_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                if not live_pid():
                    break
                time.sleep(0.1)
            else:
                raise SystemExit('Previous watchdog did not exit')
        if action == 'stop':
            print('Watchdog stopped; running tunnel is left untouched')
            return
        action = 'start'
    if action == 'start':
        pid = live_pid()
        if pid:
            print('Tunnel watchdog already running:', pid)
            return
        with (DIRECTORY / 'daemon.stderr.log').open('ab') as log:
            child = subprocess.Popen(['/usr/bin/python3', SCRIPT, 'daemon'],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     cwd='/', start_new_session=True, close_fds=True)
        for _ in range(20):
            pid = live_pid()
            if pid:
                print('Tunnel watchdog running:', pid)
                return
            if child.poll() is not None:
                raise SystemExit('Watchdog failed to start; inspect daemon.stderr.log')
            time.sleep(0.1)
        raise SystemExit('Watchdog did not become ready')
    if action not in ('daemon', 'once'):
        raise SystemExit('Usage: onec-v8sm-watchdog start|stop|restart|status|pause|resume|once')
    with (DIRECTORY / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Watchdog already owns the lock')
        if action == 'once':
            print(json.dumps(check(), indent=2))
            return
        (DIRECTORY / 'pid').write_text(str(os.getpid()) + '\n')
        while True:
            try:
                check()
            except Exception as error:
                state('check_failed', message=str(error)[:400])
            time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
