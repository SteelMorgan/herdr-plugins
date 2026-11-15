#!/usr/bin/python3
"""Guard only the dedicated reverse SSH listener; never probe WS or touch 1C."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import time

USER = 'onec-v8sm-tunnel'
PEER = '10.77.90.2'
ROOT = Path('/run/onec-ssh-tunnel-guard')
STALE_MS = 180_000
CONFIRM_SECONDS = 40
CONFIRM_CHECKS = 3
COOLDOWN = 120


def command(*args):
    return subprocess.check_output(args, text=True, timeout=8)


def process_identity(pid):
    p = Path('/proc') / str(pid)
    status = (p / 'status').read_text()
    uid = int(re.search(r'^Uid:\s+(\d+)', status, re.M)[1])
    comm = (p / 'comm').read_text().strip()
    # stat's comm may contain spaces/parentheses; starttime is field 22.
    start = (p / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    return {'pid': pid, 'uid': uid, 'comm': comm, 'start_ticks': start}


def parse_listener(text):
    lines = text.strip().splitlines()
    if len(lines) != 1:
        return None
    columns = lines[0].split()
    if len(columns) < 5 or columns[3] != '127.0.0.1:4000':
        return None
    pids = set(map(int, re.findall(r'pid=(\d+)', lines[0])))
    return next(iter(pids)) if len(pids) == 1 else None


def parse_transport(text, pid):
    records = []
    for line in text.splitlines():
        if line.startswith((' ', '\t')) and records:
            records[-1] += ' ' + line.strip()
        elif line.strip():
            records.append(line)
    matches = [line for line in records if re.search(r'pid=' + str(pid) + r'[,)]', line)]
    if len(matches) != 1:
        return None
    line = matches[0]
    columns = line.split()
    if len(columns) < 5 or columns[0] != 'ESTAB' or columns[3] != '10.77.20.10:22' or not columns[4].startswith(PEER + ':'):
        return None
    values = {'send_queue': int(columns[2])}
    for key in ('lastrcv', 'lastack', 'backoff', 'rto'):
        m = re.search(r'\b' + key + r':(\d+)\b', line)
        if m:
            values[key] = int(m[1])
    m = re.search(r'\bino:(\d+)\b', line)
    if not m:
        return None
    values['socket_inode'] = m[1]
    return values


def snapshot():
    pid = parse_listener(command('/usr/bin/ss', '-H', '-lntp', 'sport = :4000'))
    if pid is None:
        return {'state': 'no_unique_dedicated_listener'}
    identity = process_identity(pid)
    if identity['uid'] != pwd.getpwnam(USER).pw_uid or identity['comm'] not in ('sshd-session', 'sshd'):
        return {'state': 'foreign_listener_untouched'}
    transport = parse_transport(command('/usr/bin/ss', '-H', '-ntpei', 'sport = :22'), pid)
    if transport is None:
        return {'state': 'transport_identity_unproven'}
    fd_target = 'socket:[' + transport['socket_inode'] + ']'
    if not any(os.readlink(p) == fd_target for p in (Path('/proc') / str(pid) / 'fd').iterdir()):
        return {'state': 'socket_identity_unproven'}
    key = ':'.join([Path('/proc/sys/kernel/random/boot_id').read_text().strip(), str(pid), identity['start_ticks'], transport['socket_inode']])
    btime = int(re.search(r'^btime (\d+)', Path('/proc/stat').read_text(), re.M).group(1))
    started = btime + int(identity['start_ticks']) / os.sysconf('SC_CLK_TCK')
    return {'state': 'observed', 'identity': identity, 'transport': transport,
            'key': key, 'session_started_epoch': started}


def stale(s, container_started=None):
    t = s.get('transport', {})
    if s.get('state') != 'observed':
        return False
    # Classic proof: server is trying to reach a dead client.
    if (t.get('send_queue', 0) > 0 and t.get('lastrcv', 0) >= STALE_MS
            and t.get('lastack', 0) >= STALE_MS and t.get('backoff', 0) >= 3):
        return True
    # Idle orphan (incident 2026-11-15): the session predates the current
    # tunnel container start and stayed silent beyond STALE_MS. A healthy
    # tunnel answers the scoped 15s keepalive, so lastrcv cannot reach
    # STALE_MS while the client lives; empty send_queue alone is not proof.
    return (container_started is not None
            and s.get('session_started_epoch', 0) < container_started - 5
            and t.get('lastrcv', 0) >= STALE_MS and t.get('lastack', 0) >= STALE_MS)


def decide(s, previous, now, container_started=None):
    if not stale(s, container_started):
        return {'state': 'healthy_or_unproven', 'count': 0}, False
    if now - previous.get('last_termination', -COOLDOWN) < COOLDOWN:
        return {**previous, 'state': 'cooldown'}, False
    same = previous.get('key') == s['key'] and 0 <= now - previous.get('last_check', 0) <= 90
    count = previous.get('count', 0) + 1 if same else 1
    first = previous['first_seen'] if same else now
    result = {**previous, 'state': 'suspect', 'key': s['key'], 'count': count,
              'first_seen': first, 'last_check': now}
    return result, count >= CONFIRM_CHECKS and now - first >= CONFIRM_SECONDS


def terminate(s, container_started=None):
    # pidfd pins the actual process, protecting against PID reuse between checks.
    with os.fdopen(os.pidfd_open(s['identity']['pid']), 'rb', closefd=True) as fd:
        current = snapshot()
        if current.get('key') != s['key'] or current.get('identity') != s['identity'] or not stale(current, container_started):
            return False
        signal.pidfd_send_signal(fd.fileno(), signal.SIGTERM)
        return True


def main():
    args = sys.argv[1:]
    action = args[0] if args else 'inspect'
    container_started = None
    if '--container-started' in args:
        i = args.index('--container-started')
        container_started = datetime.datetime.fromisoformat(
            args[i + 1].replace('Z', '+00:00')).timestamp()
    if action not in ('inspect', 'check') or os.geteuid() != 0:
        raise SystemExit('Usage (root): onec-ssh-tunnel-guard inspect|check [--container-started ISO]')
    s = snapshot()
    out = {'observed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), **s}
    if action == 'check':
        ROOT.mkdir(mode=0o700, exist_ok=True)
        with (ROOT / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            p = ROOT / 'state.json'
            old = json.loads(p.read_text()) if p.exists() else {}
            now = time.monotonic()
            decision, reap = decide(s, old, now, container_started)
            # Preserve cooldown across healthy observations.
            if 'last_termination' in old:
                decision.setdefault('last_termination', old['last_termination'])
            if reap:
                if terminate(s, container_started):
                    decision.update(state='terminated_stale_ssh', last_termination=now, count=0)
                else:
                    decision.update(state='identity_changed_untouched', count=0)
            temp = p.with_suffix('.tmp')
            temp.write_text(json.dumps(decision))
            temp.replace(p)
            out['guard'] = decision
    print(json.dumps(out))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print(json.dumps({'state': 'inspection_failed_no_action'}))
        sys.exit(1)
