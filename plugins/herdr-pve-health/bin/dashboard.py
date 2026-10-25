#!/usr/bin/python3
"""One lightweight SSH sample/30s; slower groups cached independently on the PC."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty

ROOT=Path(__file__).resolve().parents[1]
CONFIG_DIR=Path(os.environ.get('HERDR_PLUGIN_CONFIG_DIR',str(Path.home()/'.config/herdr/plugins/config/herdr.pve-health')))
try:CONFIG=json.loads((CONFIG_DIR/'config.json').read_text())
except (OSError,ValueError):CONFIG={}
CONFIG_ID=hashlib.sha256(json.dumps(CONFIG,sort_keys=True).encode()).hexdigest()
SSH_CONFIG=Path(CONFIG.get('ssh_config',str(Path.home()/'.ssh/config'))).expanduser()
HOSTS=CONFIG.get('hosts',{'pve':'gbig-pve','agents':'gbig-agents','vpn':'gbig-vpn','onec':'gbig-1c-app'})
STATE=Path.home()/'.local/state/herdr-pve-health'
WINDOWS_BACKUP=Path(CONFIG['windows_backup_file']) if CONFIG.get('windows_backup_file') else None
INTERVALS={'fast':30,'storage':60,'vms':120,'services':120,'errors':120,
           'backups':300,'apps':300,'agents':300,'vpn':300,'onec':300,'smart':900}
ESC='\x1b[';RESET=ESC+'0m';BG=ESC+'48;2;30;30;46m'
TEXT=ESC+'38;2;202;211;245m';MUTED=ESC+'38;2;127;132;156m'
GREEN=ESC+'38;2;166;227;161m';RED=ESC+'38;2;243;139;168m'
YELLOW=ESC+'38;2;249;226;175m';BLUE=ESC+'38;2;137;180;250m'

AGENTS_PROBE="""import subprocess,json
names=['gbig-hermes-gateway.service','gbig-hermes-office.service',
'gbig-office-runner.socket','gbig-skill-distill.timer','gbig-office-scheduler.timer']
p=subprocess.run(['systemctl','show',*names,'--property=Id','--property=ActiveState'],capture_output=True,text=True,timeout=5)
if p.returncode:raise RuntimeError('systemctl failed')
rows=[dict(l.split('=',1) for l in b.splitlines() if '=' in l) for b in p.stdout.strip().split('\\n\\n')]
print(json.dumps(rows))
"""
VPN_PROBE="""import subprocess,json,time
p=subprocess.run(['wg','show','interfaces'],capture_output=True,text=True,timeout=3)
if p.returncode:raise RuntimeError('WireGuard status unavailable')
interfaces=p.stdout.split()
p=subprocess.run(['wg','show','all','latest-handshakes'],capture_output=True,text=True,timeout=3)
if p.returncode:raise RuntimeError('WireGuard handshakes unavailable')
stamps=[int(l.split()[-1]) for l in p.stdout.splitlines() if l.strip()]
print(json.dumps({'interfaces':len(interfaces),'peers':len(stamps),'recent':sum(t>0 and time.time()-t<180 for t in stamps),'latest':max(stamps,default=0)}))
"""
ONEC_PROBE="""import socket,json
try:
 s=socket.create_connection(('127.0.0.1',5432),timeout=2);s.close();ok=True
except OSError:ok=False
print(json.dumps({'postgres_tcp':ok}))
"""


def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.health-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def due(groups,now):
    return [key for key,interval in INTERVALS.items()
            if now-groups.get(key,{}).get('received_at',0)>=
            (min(interval,60) if groups.get(key,{}).get('error') else interval)]


def ssh(target,script,args=()):
    command='sudo -n python3 -'+''.join(' '+shlex.quote(a) for a in args)
    start=time.monotonic()
    p=subprocess.run(['ssh','-F',str(SSH_CONFIG),'-o','ConnectTimeout=5',
                      '-o','BatchMode=yes',target,command],input=script,
                     text=True,capture_output=True,timeout=45)
    if p.returncode:raise RuntimeError(f'SSH {target}: exit={p.returncode} '+p.stderr.strip()[:120])
    return json.loads(p.stdout),(time.monotonic()-start)*1000


def metrics(current,previous):
    """Rates over successive samples; boot change/counter reset never becomes a spike."""
    out={'cpu':None,'wait':None,'net_rx':None,'net_tx':None,'disk':{}}
    if not previous or current.get('boot')!=previous.get('boot'):return out
    dt=current.get('uptime',0)-previous.get('uptime',0)
    if dt<=0:return out
    ticks=[a-b for a,b in zip(current['ticks'],previous['ticks'])]
    total=sum(ticks)
    if total>0 and all(t>=0 for t in ticks):
        out['cpu']=100*(total-ticks[3]-ticks[4])/total;out['wait']=100*ticks[4]/total
    common=set(current.get('net',{})) & set(previous.get('net',{}))
    if common:
        delta={key:sum(current['net'][n][key]-previous['net'][n][key] for n in common) for key in ['rx','tx']}
        if min(delta.values())>=0:out.update(net_rx=delta['rx']/dt,net_tx=delta['tx']/dt)
    for name,values in current.get('disks',{}).items():
        old=previous.get('disks',{}).get(name)
        if not old:continue
        d=[a-b for a,b in zip(values,old)]
        if min(d)<0:continue
        ops=d[0]+d[3]
        out['disk'][name]={'read':d[1]*512/dt,'write':d[4]*512/dt,
                           'await_ms':(d[2]+d[5])/ops if ops else 0,
                           'busy':min(100,d[6]/(10*dt))}
    return out


def smart_values(item):
    d=item.get('data',{});rows=d.get('ata_smart_attributes',{}).get('table',[])
    attrs={x['name']:x for x in rows}
    def raw(*names):
        return next((attrs[n].get('raw',{}).get('value') for n in names if n in attrs),None)
    nv=d.get('nvme_smart_health_information_log',{})
    return {'passed':d.get('smart_status',{}).get('passed'),
            'temperature':d.get('temperature',{}).get('current',nv.get('temperature')),
            'life_raw':raw('SSD_Life_Left','Percent_Lifetime_Remain'),
            'used_percent':nv.get('percentage_used'),'written_gib':raw('Lifetime_Writes_GiB'),
            'realloc_events':raw('Reallocated_Event_Count'),'pending':raw('Current_Pending_Sector'),
            'uncorrectable':raw('Reported_Uncorrect','Offline_Uncorrectable'),
            'crc':raw('SATA_CRC_Error_Count','UDMA_CRC_Error_Count'),
            'write_protected':raw('Write_Protect_Mode'),'media_errors':nv.get('media_errors'),
            'unsafe':nv.get('unsafe_shutdowns',raw('Unsafe_Shutdown_Count')),
            'hours':d.get('power_on_time',{}).get('hours'),
            'attribute_failures':[a['name'] for a in rows if str(a.get('when_failed','')).lower()=='now']}


class Store:
    def __init__(self):
        self.lock=threading.Lock();self.busy=False;self.last_attempt=0
        try:self.data=json.loads((STATE/'cache.json').read_text())
        except (OSError,ValueError):self.data={'groups':{}}
        if self.data.get('config_id')!=CONFIG_ID:self.data={'groups':{},'config_id':CONFIG_ID}
        self.data.setdefault('groups',{})
    def snapshot(self):
        with self.lock:return json.loads(json.dumps(self.data)),self.busy
    def collect(self):
        with self.lock:
            if self.busy:return
            self.busy=True;self.last_attempt=time.time()
            needed=due(self.data['groups'],self.last_attempt)
        try:
            jobs={};main=[n for n in needed if n not in ['agents','vpn','onec']]
            with ThreadPoolExecutor(max_workers=3) as pool:
                script='SETTINGS = '+repr({k:CONFIG[k] for k in ['tcp_checks','https_check','dns_check'] if k in CONFIG})+'\n'+(ROOT/'bin/probe.py').read_text()
                if main:jobs[pool.submit(ssh,HOSTS['pve'],script,main)]='pve'
                if 'agents' in needed:jobs[pool.submit(ssh,HOSTS['agents'],AGENTS_PROBE)]='agents'
                if 'vpn' in needed:jobs[pool.submit(ssh,HOSTS['vpn'],VPN_PROBE)]='vpn'
                if 'onec' in needed:jobs[pool.submit(ssh,HOSTS['onec'],ONEC_PROBE)]='onec'
                for future in as_completed(jobs):
                    name=jobs[future];now=time.time()
                    try:
                        result,elapsed=future.result()
                        with self.lock:
                            if name=='pve':
                                old=self.data['groups'].get('fast',{}).get('data')
                                for key,group in result['groups'].items():
                                    group['received_at']=now
                                    if key=='fast' and group.get('data'):
                                        self.data['rates']=metrics(group['data'],old)
                                    self.data['groups'][key]=group
                                self.data.update(ssh_error=None,last_success=now,ssh_ms=round(elapsed,1),
                                                 probe_ms=result['probe_ms'],cpu_ms=result['cpu_ms'])
                            else:self.data['groups'][name]={'data':result,'error':None,'received_at':now}
                    except Exception as e:
                        with self.lock:
                            if name=='pve':self.data['ssh_error']=str(e)[:180]
                            else:self.data['groups'][name]={'data':None,'error':str(e)[:140],'received_at':now}
            try:windows=json.loads(WINDOWS_BACKUP.read_text()) if WINDOWS_BACKUP else None
            except (OSError,ValueError):windows=None
            with self.lock:
                self.data['windows_backup']=windows
                atomic_json(STATE/'cache.json',self.data)
        finally:
            with self.lock:self.busy=False


def n(value,precision=1):return '--' if value is None else f'{value:.{precision}f}'
def size(value):
    if value is None:return '--'
    for unit in ['B','KiB','MiB','GiB','TiB']:
        if abs(value)<1024 or unit=='TiB':return f'{value:.1f}{unit}'
        value/=1024
def age(stamp,now=None):
    if not stamp:return '--'
    seconds=max(0,(now or time.time())-stamp)
    if seconds<120:return f'{seconds:.0f}s'
    if seconds<7200:return f'{seconds/60:.0f}m'
    return f'{seconds/3600:.1f}h'
def unit_name(name):return name.removesuffix('.service').removesuffix('.socket').removesuffix('.timer')


def lines(data,now,width):
    groups=data.get('groups',{});out=[]
    def add(text,color=TEXT):out.append((text,color))
    def group(key):return groups.get(key,{}).get('data')
    def stamp(key):
        value=groups.get(key,{});suffix=' ERROR' if value.get('error') else ''
        if not value.get('received_at'):return 'waiting'
        return age(value['received_at'],now)+suffix
    def fresh(key):
        value=groups.get(key,{})
        return bool(value.get('received_at')) and not value.get('error') and now-value['received_at']<=INTERVALS[key]*2+15
    fast=group('fast') or {};mem=fast.get('mem',{});rates=data.get('rates',{})
    ram_used=mem['MemTotal']-mem['MemAvailable'] if 'MemTotal' in mem and 'MemAvailable' in mem else None
    swap_used=mem['SwapTotal']-mem['SwapFree'] if 'SwapTotal' in mem and 'SwapFree' in mem else None
    online=fresh('fast') and not data.get('ssh_error')
    add(f" DEV SERVER | {fast.get('hostname',HOSTS.get('pve','--'))} | {'LIVE' if online else 'CONNECTING / STALE'} | sample {stamp('fast')}",GREEN if online else YELLOW)
    add(f" CPU {n(rates.get('cpu'))}%  iowait {n(rates.get('wait'))}%  load {'/'.join(n(x) for x in fast.get('load',[])) or '--'}  "
        f"RAM {size(ram_used)}/{size(mem.get('MemTotal'))}  "
        f"swap {size(swap_used)}  uptime {n(fast['uptime']/3600 if 'uptime' in fast else None)}h")
    temps=fast.get('sensors',[]);peak=max((x['value'] for x in temps if x['label'].startswith(('Core','Package'))),default=None)
    psi=fast.get('psi',{});net=fast.get('net',{})
    add(f" CPU temp max {n(peak)}C | PSI cpu/mem/io {'/'.join(n(psi.get(x)) for x in ['cpu','memory','io'])}% | "
        f"NET RX {size(rates.get('net_rx'))}/s TX {size(rates.get('net_tx'))}/s err/drop {sum(x['errors'] for x in net.values()) if net else '--'}/{sum(x['drops'] for x in net.values()) if net else '--'}")
    storage=group('storage') or {}
    for fs in storage.get('filesystems',[]):
        add(f" FS {fs['mount']:<8} {n(fs['percent'])}%  {size(fs['used'])} used / {size(fs['free'])} free  inodes {n(fs['inode_percent'])}%  {'READ-ONLY' if fs['readonly'] else 'rw'}",
            RED if fs['readonly'] or fs['percent']>=95 else YELLOW if fs['percent']>=85 else TEXT)
    for pool in storage.get('thin',[]):add(f" LVM {pool['vg_name']}/{pool['lv_name']} data {pool['data_percent']}% metadata {pool['metadata_percent']}%")
    add(f" SMART | interval 15m | sample {stamp('smart')} | life(raw) is vendor value, '--' = not reported",BLUE)
    for disk in group('smart') or []:
        s=smart_values(disk);ok=s['passed'];status='PASS' if ok is True else 'FAIL' if ok is False else 'UNKNOWN'
        color=RED if ok is False or s['attribute_failures'] or s['write_protected'] else GREEN if ok is True and fresh('smart') else YELLOW
        life=f"used {s['used_percent']}%" if s['used_percent'] is not None else f"life(raw) {s['life_raw'] if s['life_raw'] is not None else '--'}"
        add(f" {disk['device']} {disk.get('model','')} SN {disk.get('serial','--')} | {status} {n(s['temperature'],0)}C | {life} | writes {size(s['written_gib']*1024**3) if s['written_gib'] is not None else '--'}",color)
        io=rates.get('disk',{}).get(disk['device'],{})
        def count(key):return '--' if s[key] is None else str(s[key])
        add(f"   re.ev {count('realloc_events')} pend {count('pending')} unc {count('uncorrectable')} CRC {count('crc')} unsafe {count('unsafe')} | "
            f"R/W {size(io.get('read'))}/{size(io.get('write'))}/s lat {n(io.get('await_ms'))}ms busy {n(io.get('busy'))}%",
            YELLOW if any(s[k] for k in ['realloc_events','pending','uncorrectable','crc','media_errors']) else TEXT)
    if not group('smart'):add(' SMART unavailable / waiting for read-only probe',YELLOW)
    add(f" VIRTUAL MACHINES | sample {stamp('vms')} | RAM = host QEMU RSS / configured vRAM",BLUE)
    for vm in sorted(group('vms') or [],key=lambda x:x.get('vmid',0)):
        add(f" {vm['vmid']:>3} {vm.get('name',''):<16.16} {vm.get('status','unknown'):<8} CPU {n(vm.get('cpu')*100 if vm.get('cpu') is not None else None):>5}% "
            f"RAM {size(vm.get('mem')):>9}/{size(vm.get('maxmem')):<9} uptime {n((vm.get('uptime') or 0)/3600)}h",GREEN if vm.get('status')=='running' and fresh('vms') else YELLOW)
    svc=group('services') or {};units=svc.get('units',[]);good=sum(x.get('ActiveState')=='active' for x in units)
    bad=[unit_name(x.get('Id','?')) for x in units if x.get('ActiveState')!='active']
    add(f" HOST SERVICES {good}/{len(units)} active | failed: {', '.join(svc.get('failed',[])) or 'none'}"+(' | down: '+','.join(bad) if bad else ''),YELLOW if bad or svc.get('failed') or not fresh('services') else TEXT)
    agent_units=group('agents') or [];agent_bad=[unit_name(x.get('Id','?')) for x in agent_units if x.get('ActiveState')!='active']
    app=group('apps') or {};http=app.get('ERA HTTPS',{})
    add(f" APPS(5m) Admin/Office/runner/timers {sum(x.get('ActiveState')=='active' for x in agent_units)}/{len(agent_units)} | "
        f"1C TCP {'UP' if app.get('1C TCP') else '--'} PG TCP {'UP' if (group('onec') or {}).get('postgres_tcp') else '--'} "
        f"ERA {http.get('status') or '--'}/TLS{'ok' if http.get('tls') else '--'} ({http.get('expected','--')} expected) DNS {'ok' if app.get('host DNS') else '--'}",
        YELLOW if agent_bad or not fresh('apps') or not fresh('agents') or not fresh('onec') or (group('onec') or {}).get('postgres_tcp') is False or any(app.get(k) is False for k in ['1C TCP','host DNS']) or http.get('status')!=http.get('expected') else TEXT)
    vpn=group('vpn') or {}
    add(f" VPN(5m) interfaces {vpn.get('interfaces','--')} peers {vpn.get('peers','--')} recent {vpn.get('recent','--')} latest handshake {age(vpn.get('latest'),now)} | idle peers are normal",TEXT if fresh('vpn') and vpn.get('interfaces') else YELLOW)
    backup=group('backups') or {};rows=backup.get('vms',[])
    stale=any(not x.get('completed_at') or now-x['completed_at']>36*3600 or not x.get('identity_matches') for x in rows)
    add(' BACKUP VM ages: '+' | '.join(f"{x['vmid']} {age(x.get('completed_at'),now)}" for x in rows)+f" | host {age(backup.get('host_completed_at'),now)}",YELLOW if stale or not fresh('backups') else TEXT)
    jobs=backup.get('jobs',[]);enabled=sum(bool(x.get('enabled',True)) for x in jobs)
    add(f" BACKUP jobs {enabled}/{len(jobs)} enabled | published {age(backup.get('published_at'),now)} | checksums recorded at backup, age limit 36h",YELLOW if not enabled or backup.get('jobs_error') else MUTED)
    win=data.get('windows_backup') or {};same=win.get('ServerGeneratedAt')==backup.get('published_at') and win.get('Status')=='success'
    add(f" WINDOWS D: {win.get('Status','unknown')} {win.get('CompletedAt','--')[:19]} | verified {win.get('VerifiedFiles','--')}/6 | {'latest published set copied' if same else 'not matched to current export'}",TEXT if same else YELLOW)
    errs=group('errors') or {};counts=errs.get('counts',{})
    add(f" KERNEL(30m/max80) I/O {counts.get('io','--')} OOM {counts.get('oom','--')} resets {counts.get('reset','--')} read-only {counts.get('readonly','--')} | sample {stamp('errors')}",RED if any(counts.values()) else TEXT if fresh('errors') else YELLOW)
    if data.get('ssh_error'):add(' SSH ERROR: '+data['ssh_error'],RED)
    errors=[f'{key}: {v["error"]}' for key,v in groups.items() if v.get('error')]
    if errors:add(' PROBE ERROR: '+'; '.join(errors),YELLOW)
    add(f" poll: host 30s | FS 60s | VM/services/log 120s | backup/apps 5m | SMART 15m | SSH {n(data.get('ssh_ms'),0)}ms probe CPU {n(data.get('cpu_ms'),0)}ms",MUTED)
    return out


def render(data,busy,width,height):
    rows=lines(data,time.time(),width);footer=(' r refresh light metrics | q close | collecting...' if busy else ' r refresh light metrics | q close | SSH read-only, no disk self-tests')
    limit=max(1,height-1)
    if len(rows)>limit:
        # Hide cosmetic headings before dropping measurements on short terminals.
        rows=[r for r in rows if not r[0].startswith((' SMART |',' VIRTUAL MACHINES |',' BACKUP jobs'))]
    if len(rows)>limit:rows=rows[:max(1,limit-1)]+[(' More rows available: increase pane height',YELLOW)]
    rows=rows[:limit]+[('',TEXT)]*max(0,limit-len(rows))
    return ESC+'H'+'\r\n'.join(BG+color+text[:width].ljust(width)+RESET for text,color in rows+[(footer,MUTED)])+ESC+'J'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--diagnose',action='store_true');args=parser.parse_args()
    store=Store()
    if args.diagnose:
        store.collect();data,_=store.snapshot();print(json.dumps(data,indent=2));return
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise SystemExit('Dashboard requires a PTY')
    previous=termios.tcgetattr(sys.stdin.fileno());tty.setcbreak(sys.stdin.fileno())
    try:
        sys.stdout.write(ESC+'?1049h'+ESC+'?25l');sys.stdout.flush()
        while True:
            now=time.time();data,busy=store.snapshot()
            if not busy and now-store.last_attempt>=30:
                threading.Thread(target=store.collect,daemon=True).start()
            columns,rows=shutil.get_terminal_size((129,28))
            sys.stdout.write(render(data,busy,columns,rows));sys.stdout.flush()
            ready,_,_=select.select([sys.stdin],[],[],1)
            if ready:
                key=os.read(sys.stdin.fileno(),1)
                if key in [b'q',b'Q',b'\x03',b'\x04']:break
                if key.lower()==b'r' and not busy and time.time()-store.last_attempt>=5:
                    # Fast only; cached SMART/VM/backup timestamps are not invalidated.
                    with store.lock:store.data['groups'].get('fast',{})['received_at']=0
                    threading.Thread(target=store.collect,daemon=True).start()
    finally:
        termios.tcsetattr(sys.stdin.fileno(),termios.TCSADRAIN,previous)
        sys.stdout.write(RESET+ESC+'?25h'+ESC+'?1049l');sys.stdout.flush()


if __name__=='__main__':main()
