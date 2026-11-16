#!/usr/bin/env python3
"""disk-map — полная карта диска хоста для анализа роста и «мусора».

Низкий IO-приоритет (ionice idle + nice 19), только stdlib. Собирает:
  * монтирования (df/lsblk), inode;
  * деревья du по каждому реальному ФС (depth 2/3, top-N);
  * крупнейшие файлы (>100M) и недавние крупные (>200M, mtime <=30 дней);
  * удалённые, но открытые файлы (утечки места через /proc);
  * journal, apt-кэш, кэши пользователей, podman/docker, старые ядра;
  * diff с предыдущим запуском (latest.json), если он есть.

Результат: ~/.local/state/disk-map/disk-map-<ts>.json и .txt (+ latest.json).
Запуск: sudo disk-map   (или python3 disk-map.py). Ничего не удаляет.
"""
import datetime
import json
import os
import pwd
import shutil
import subprocess
import sys
import time

TOP_TREE = 60
TOP_FILES = 120
PSEUDO = {'proc', 'sysfs', 'devtmpfs', 'devpts', 'cgroup', 'cgroup2', 'pstore',
          'securityfs', 'debugfs', 'tracefs', 'configfs', 'fusectl', 'mqueue',
          'hugetlbfs', 'binfmt_misc', 'autofs', 'rpc_pipefs', 'nsfs', 'overlay',
          'squashfs', 'ramfs'}


def run(cmd, timeout=3600):
    pre = []
    if shutil.which('ionice'):
        pre += ['ionice', '-c3']
    if shutil.which('nice'):
        pre += ['nice', '-n19']
    try:
        p = subprocess.run(pre + cmd, capture_output=True, text=True,
                           timeout=timeout, errors='replace')
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, '', 'timeout'
    except FileNotFoundError as e:
        return -1, '', str(e)


def du(path, depth):
    rc, out, err = run(['du', '-x', '-B1', '--max-depth=%d' % depth, path])
    rows = []
    for line in out.splitlines():
        parts = line.split('\t', 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append({'bytes': int(parts[0]), 'path': parts[1]})
    rows.sort(key=lambda r: -r['bytes'])
    return rows[:TOP_TREE], err.strip()


def find_big(path, min_size, recent_days=None):
    cmd = ['find', path, '-xdev', '-type', 'f', '-size', '+%dM' % min_size,
           '-printf', '%s\t%T@\t%p\n']
    if recent_days is not None:
        cmd[5:5] = ['-mtime', '-%d' % recent_days]
    rc, out, err = run(cmd)
    rows = []
    for line in out.splitlines():
        parts = line.split('\t', 2)
        if len(parts) == 3 and parts[0].isdigit():
            rows.append({'bytes': int(parts[0]), 'mtime': float(parts[1]),
                         'path': parts[2]})
    rows.sort(key=lambda r: -r['bytes'])
    return rows[:TOP_FILES]


def deleted_open():
    rows = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        fd_dir = '/proc/%s/fd' % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        try:
            with open('/proc/%s/comm' % pid) as f:
                comm = f.read().strip()
        except OSError:
            comm = '?'
        for fd in fds:
            p = os.path.join(fd_dir, fd)
            try:
                target = os.readlink(p)
                if '(deleted)' not in target:
                    continue
                size = os.stat(p).st_size
            except OSError:
                continue
            if size > 10 * 1024 * 1024:
                rows.append({'bytes': size, 'pid': int(pid), 'comm': comm,
                             'path': target.replace(' (deleted)', '')})
    rows.sort(key=lambda r: -r['bytes'])
    return rows[:60]


def mounts():
    rc, out, err = run(['df', '-B1', '--output=source,fstype,size,used,avail,pcent,target'])
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        src, fstype = parts[0], parts[1]
        target = ' '.join(parts[6:])
        try:
            size, used, avail = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        rows.append({'source': src, 'fstype': fstype, 'bytes': size,
                     'used': used, 'avail': avail, 'target': target})
    return rows


def home_dirs():
    homes = []
    for ent in pwd.getpwall():
        if ent.pw_dir.startswith('/home/') or ent.pw_dir == '/root':
            if os.path.isdir(ent.pw_dir):
                homes.append((ent.pw_name, ent.pw_dir))
    return homes


def podman_df(user):
    env = dict(os.environ)
    try:
        ent = pwd.getpwnam(user)
    except KeyError:
        return None
    env['XDG_RUNTIME_DIR'] = '/run/user/%d' % ent.pw_uid
    cmd = ['sudo', '-u', user, 'env', 'XDG_RUNTIME_DIR=' + env['XDG_RUNTIME_DIR'],
           'podman', 'system', 'df']
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           errors='replace', env=env)
        return p.stdout.strip() or p.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def human(n):
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if abs(n) < 1024 or unit == 'T':
            return '%.1f%s' % (n, unit)
        n /= 1024.0


def main():
    started = time.time()
    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    sudo_user = os.environ.get('SUDO_USER')
    owner = pwd.getpwnam(sudo_user) if sudo_user else pwd.getpwuid(os.getuid())
    out_dir = os.path.join(owner.pw_dir, '.local', 'state', 'disk-map')
    os.makedirs(out_dir, exist_ok=True)

    data = {'ts': ts, 'host': os.uname().nodename, 'kernel': os.uname().release,
            'generated_at': time.time()}
    rc, out, _ = run(['lsblk', '-o', 'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT'])
    data['lsblk'] = out.strip()
    rc, out, _ = run(['df', '-i', '-x', 'tmpfs', '-x', 'devtmpfs'])
    data['df_inodes'] = out.strip()

    mnt = mounts()
    data['mounts'] = mnt
    real = [m for m in mnt if m['fstype'] not in PSEUDO and m['bytes'] > 0]
    tmpfs = [m for m in mnt if m['fstype'] == 'tmpfs' and m['used'] > 0]

    data['trees'] = {}
    data['top_files'] = {}
    data['recent_big_files'] = {}
    for m in real:
        t = m['target']
        depth = 3 if t in ('/', '/var', '/home', '/usr', '/opt', '/srv') else 2
        tree, err = du(t, depth)
        data['trees'][t] = tree
        data['top_files'][t] = find_big(t, 100)
        data['recent_big_files'][t] = find_big(t, 200, recent_days=30)
    data['tmpfs_usage'] = [{'target': m['target'], 'used': m['used'],
                            'tree': du(m['target'], 1)[0]} for m in tmpfs]

    data['deleted_open_files'] = deleted_open()
    rc, out, _ = run(['journalctl', '--disk-usage'])
    data['journal_usage'] = out.strip()
    rc, out, _ = run(['dpkg-query', '-W', '-f=${Package}\t${Version}\n', 'linux-image-*'])
    data['kernels'] = out.strip()

    data['homes'] = {}
    for user, home in home_dirs():
        entry = {'tree': du(home, 3)[0]}
        cache = os.path.join(home, '.cache')
        if os.path.isdir(cache):
            entry['cache'] = du(cache, 1)[0]
        data['homes'][user] = entry
    rc, out, _ = run(['du', '-s', '-B1', '/var/cache/apt/archives'])
    data['apt_cache'] = out.strip()
    data['podman_df'] = podman_df(sudo_user or owner.pw_name)
    if shutil.which('docker'):
        rc, out, _ = run(['docker', 'system', 'df'])
        data['docker_df'] = out.strip()

    prev_path = os.path.join(out_dir, 'latest.json')
    if os.path.isfile(prev_path):
        try:
            with open(prev_path) as f:
                prev = json.load(f)
            deltas = []
            for t, tree in data['trees'].items():
                old = {r['path']: r['bytes'] for r in prev.get('trees', {}).get(t, [])}
                for r in tree:
                    if r['path'] in old:
                        d = r['bytes'] - old[r['path']]
                        if abs(d) > 100 * 1024 * 1024:
                            deltas.append({'path': r['path'], 'delta': d,
                                           'bytes': r['bytes']})
            deltas.sort(key=lambda r: -abs(r['delta']))
            data['delta_vs_previous'] = {'previous_ts': prev.get('ts'),
                                         'rows': deltas[:60]}
        except (ValueError, OSError):
            pass

    json_path = os.path.join(out_dir, 'disk-map-%s.json' % ts)
    txt_path = os.path.join(out_dir, 'disk-map-%s.txt' % ts)
    with open(json_path, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    shutil.copyfile(json_path, prev_path)

    lines = ['disk-map %s @ %s (kernel %s), %.0fs' %
             (ts, data['host'], data['kernel'], time.time() - started), '']
    lines.append('== mounts ==')
    for m in mnt:
        lines.append('%-12s %-10s %8s used %8s / %8s  %s' %
                     (m['fstype'], m['source'][:12], human(m['used']),
                      human(m['avail']), human(m['bytes']), m['target']))
    lines.append('\n== lsblk ==\n' + data['lsblk'])
    for t, tree in data['trees'].items():
        lines.append('\n== du %s ==' % t)
        for r in tree[:40]:
            lines.append('%10s  %s' % (human(r['bytes']), r['path']))
    for t, files in data['top_files'].items():
        lines.append('\n== biggest files %s ==' % t)
        for r in files[:40]:
            mt = datetime.datetime.fromtimestamp(r['mtime']).strftime('%Y-%m-%d')
            lines.append('%10s  %s  %s' % (human(r['bytes']), mt, r['path']))
    for t, files in data['recent_big_files'].items():
        if files:
            lines.append('\n== recent big files (<=30d, >200M) %s ==' % t)
            for r in files[:40]:
                mt = datetime.datetime.fromtimestamp(r['mtime']).strftime('%Y-%m-%d')
                lines.append('%10s  %s  %s' % (human(r['bytes']), mt, r['path']))
    if data['deleted_open_files']:
        lines.append('\n== deleted but open (>10M) ==')
        for r in data['deleted_open_files']:
            lines.append('%10s  pid %-7d %-16s %s' %
                         (human(r['bytes']), r['pid'], r['comm'], r['path']))
    lines.append('\n== journal ==\n' + data['journal_usage'])
    lines.append('\n== kernels ==\n' + data['kernels'])
    lines.append('\n== apt cache ==\n' + data['apt_cache'])
    for user, entry in data['homes'].items():
        lines.append('\n== home %s ==' % user)
        for r in entry['tree'][:30]:
            lines.append('%10s  %s' % (human(r['bytes']), r['path']))
        if entry.get('cache'):
            lines.append('  -- .cache --')
            for r in entry['cache'][:15]:
                lines.append('%10s  %s' % (human(r['bytes']), r['path']))
    if data.get('podman_df'):
        lines.append('\n== podman system df ==\n' + data['podman_df'])
    if data.get('docker_df'):
        lines.append('\n== docker system df ==\n' + data['docker_df'])
    if data.get('delta_vs_previous'):
        lines.append('\n== delta vs %s ==' % data['delta_vs_previous']['previous_ts'])
        for r in data['delta_vs_previous']['rows']:
            lines.append('%+10s  -> %10s  %s' %
                         (human(r['delta']), human(r['bytes']), r['path']))
    with open(txt_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    for p in (json_path, txt_path, prev_path):
        os.chown(p, owner.pw_uid, owner.pw_gid)
    print('\n'.join(lines[:60]))
    print('\nSaved: %s\nSaved: %s' % (json_path, txt_path))


if __name__ == '__main__':
    main()
