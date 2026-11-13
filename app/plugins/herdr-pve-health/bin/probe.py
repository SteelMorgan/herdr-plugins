"""Executed over SSH as root. Fixed read-only probes, no installs or disk tests."""
import glob
import http.client
import json
import os
from pathlib import Path
import re
import resource
import socket
import ssl
import subprocess
import sys
import time


def command(argv, timeout=8, as_json=False):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, LC_ALL='C', LANG='C'))
    if as_json:
        return json.loads(p.stdout), p.returncode
    if p.returncode:
        raise RuntimeError(f'{argv[0]} exit={p.returncode}')
    return p.stdout


def fast():
    mem = {row.split(':')[0]: int(row.split()[1])*1024
           for row in Path('/proc/meminfo').read_text().splitlines()}
    stat = Path('/proc/stat').read_text().splitlines()[0].split()
    disks = {}
    for line in Path('/proc/diskstats').read_text().splitlines():
        f = line.split()
        if re.fullmatch(r'sd[a-z]+|nvme\d+n\d+', f[2]):
            disks[f[2]] = [int(f[n]) for n in [3, 5, 6, 7, 9, 10, 12, 13]]
    net = {}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name, values = line.split(':', 1); name = name.strip()
        if (Path('/sys/class/net') / name / 'device').exists():
            v = [int(x) for x in values.split()]
            net[name] = {'rx': v[0], 'tx': v[8], 'errors': v[2]+v[10], 'drops': v[3]+v[11]}
    sensors = []
    for directory in Path('/sys/class/hwmon').glob('*'):
        try: driver = (directory/'name').read_text().strip()
        except OSError: continue
        for p in directory.glob('temp*_input'):
            try:
                stem=p.name.removesuffix('_input'); label=directory/(stem+'_label')
                sensors.append({'label': label.read_text().strip() if label.exists() else driver,
                                'value': int(p.read_text())/1000})
            except (OSError, ValueError): pass
    pressure = {}
    for key in ['cpu', 'memory', 'io']:
        try:
            line = (Path('/proc/pressure')/key).read_text().splitlines()[0]
            pressure[key] = float(dict(v.split('=') for v in line.split()[1:])['avg10'])
        except (OSError, ValueError, KeyError): pressure[key] = None
    return {'hostname': socket.gethostname(), 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'uptime': float(Path('/proc/uptime').read_text().split()[0]),
            'cpus': os.cpu_count(), 'ticks': list(map(int, stat[1:9])), 'mem': mem,
            'load': list(os.getloadavg()), 'disks': disks, 'net': net,
            'sensors': sensors, 'psi': pressure}


def storage():
    filesystems=[]
    for path in ['/', '/vmroot']:
        s=os.statvfs(path);used=(s.f_blocks-s.f_bfree)*s.f_frsize;free=s.f_bavail*s.f_frsize
        filesystems.append({'mount':path,'used':used,'free':free,
                            'percent':100*used/(used+free) if used+free else None,
                            'inode_percent':100*(s.f_files-s.f_ffree)/s.f_files if s.f_files else None,
                            'readonly':bool(s.f_flag & os.ST_RDONLY)})
    thin=[]; thin_error=None
    try:
        data,rc=command(['lvs','--reportformat','json','-o','vg_name,lv_name,segtype,data_percent,metadata_percent'],as_json=True)
        if rc: raise RuntimeError(f'lvs exit={rc}')
        thin=[v for r in data.get('report',[]) for v in r.get('lv',[]) if v.get('segtype')=='thin-pool']
    except Exception as e: thin_error=str(e)
    return {'filesystems':filesystems,'thin':thin,'thin_error':thin_error}


def smart():
    data,rc=command(['lsblk','-J','-b','-d','-o','NAME,TYPE,MODEL,SERIAL,SIZE,TRAN'],as_json=True)
    if rc: raise RuntimeError('lsblk failed')
    result=[]
    for disk in data.get('blockdevices',[]):
        name=disk['name']
        # Never probe nbd/loop/virtual recovery images as physical disks.
        if disk.get('type')!='disk' or not disk.get('model') or not re.fullmatch(r'sd[a-z]+|nvme\d+n\d+',name):continue
        argv=['smartctl','-j','-a']
        if disk.get('tran')=='sata':argv+=['-n','standby,3']
        obj,status=command([*argv,'/dev/'+name],as_json=True)
        result.append({'device':name,'model':disk.get('model'),'serial':disk.get('serial'),
                       'status_code':status,'data':obj})
    return result


def vms():
    data,rc=command(['pvesh','get','/cluster/resources','--type','vm','--output-format','json'],as_json=True,timeout=12)
    if rc:raise RuntimeError('PVE VM API failed')
    return [{k:d.get(k) for k in ['vmid','name','status','cpu','maxcpu','mem','maxmem','uptime','type']}
            for d in data if not d.get('template')]


def services():
    names=['pve-cluster.service','pvedaemon.service','pveproxy.service','pvestatd.service',
           'pvescheduler.service','ssh.service','smartmontools.service']
    out=command(['systemctl','show',*names,'--property=Id','--property=ActiveState','--property=SubState'])
    states=[]
    for block in out.strip().split('\n\n'):
        values=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if values:states.append(values)
    failed=command(['systemctl','--failed','--no-legend','--plain','--no-pager']).splitlines()
    return {'units':states,'failed':[line.split()[0] for line in failed if line.strip()][:15]}


def errors():
    out=command(['journalctl','-k','--since','-30 min','-p','warning','-n','80','--no-pager','-o','json'])
    patterns={'io':r'I/O error|Buffer I/O|critical medium error',
              'oom':r'out of memory|oom-kill|killed process',
              'reset':r'hard resetting link|resetting controller',
              'readonly':r'remounting filesystem read-only|remounting file system read-only'}
    counts={k:0 for k in patterns};last=[]
    for line in out.splitlines():
        try:message=json.loads(line).get('MESSAGE','')
        except ValueError:continue
        if not isinstance(message,str):continue
        matched=False
        for key,pattern in patterns.items():
            if re.search(pattern,message,re.I):counts[key]+=1;matched=True
        if matched:last.append(message[:180])
    return {'counts':counts,'last':last[-3:],'scope':'last30m/max80 kernel warnings'}



def parse_backup_tasks(index,active):
    last=None
    for line in index.splitlines():
        parts=line.split(None,2)
        if len(parts)!=3:continue
        fields=parts[0].split(':')
        if len(fields)<8 or fields[0]!='UPID' or fields[5]!='vzdump':continue
        try:ended=int(parts[1],16)
        except ValueError:continue
        if last is None or ended>=last['ended_at']:
            last={'ended_at':ended,'status':parts[2][:160]}
    running=0
    for line in active.splitlines():
        parts=line.split(None,2)
        fields=parts[0].split(':') if parts else []
        if len(parts)>=2 and parts[1]=='0' and len(fields)>=8 and fields[0]=='UPID' and fields[5]=='vzdump':running+=1
    return last,running


def task_file_tail(path):
    # Bound work even when task history grows; never launch pvesh per refresh.
    with Path(path).open('rb') as f:
        f.seek(0,2);size=f.tell();f.seek(max(0,size-128*1024))
        return f.read().decode('utf-8',errors='replace')


def backups():
    rows=[]
    for vmid in [100,110,111,200,201]:
        path=Path(f'/var/lib/gbig-backup-state/{vmid}.json')
        try:
            d=json.loads(path.read_text());s=Path(d['path']).stat()
            identity=all(d.get(k)==v for k,v in [('bytes',s.st_size),('inode',s.st_ino),('device',s.st_dev),('mtime_ns',s.st_mtime_ns)])
            rows.append({'vmid':vmid,'completed_at':d.get('completed_at'),'bytes':d.get('bytes'),
                         'identity_matches':identity,'checksum_recorded':bool(d.get('sha256'))})
        except (OSError,ValueError,KeyError):rows.append({'vmid':vmid,'error':'backup marker/file unavailable'})
    export=Path('/var/lib/gbig-backup-export/latest.json');published=None;host=None
    try:
        d=json.loads(export.read_text());published=d.get('generated_at')
        host=next((r.get('completed_at') for r in d.get('files',[]) if r.get('kind')=='proxmox-host-config'),None)
    except (OSError,ValueError):pass
    jobs=[];jobs_error=None
    try:
        # Read the verified local PVE job format; no Perl/API process per refresh.
        source=Path('/etc/pve/jobs.cfg').read_text();current=None
        for line in source.splitlines():
            if line and not line[0].isspace():
                current=None
                if line.startswith('vzdump: '):
                    current={'id':line.split(':',1)[1].strip(),'enabled':1};jobs.append(current)
            elif current is not None:
                parts=line.strip().split(None,1)
                if len(parts)==2 and parts[0] in ['enabled','schedule','vmid','storage']:
                    current[parts[0]]=int(parts[1]) if parts[0]=='enabled' else parts[1]
    except Exception as e:jobs_error=str(e)
    last_task=None;active_tasks=None;history_error=None
    try:
        last_task,active_tasks=parse_backup_tasks(task_file_tail('/var/log/pve/tasks/index'),task_file_tail('/var/log/pve/tasks/active'))
    except OSError as e:history_error=str(e)[:120]
    return {'vms':rows,'published_at':published,'host_completed_at':host,'jobs':jobs,'jobs_error':jobs_error,
            'last_task':last_task,'active_tasks':active_tasks,'task_history_error':history_error,'task_index_version':1}


def apps():
    checks={}
    settings=globals().get('SETTINGS',{})
    for name,address,port in settings.get('tcp_checks',[]):
        try:
            with socket.create_connection((address,port),timeout=2):pass
            checks[name]=True
        except OSError:checks[name]=False
    try:
        https=settings['https_check'];context=ssl.create_default_context()
        with socket.create_connection((https['address'],443),timeout=3) as raw:
            with context.wrap_socket(raw,server_hostname=https['hostname']) as conn:
                conn.sendall(('GET / HTTP/1.1\r\nHost: '+https['hostname']+'\r\nConnection: close\r\n\r\n').encode())
                first=conn.recv(4096).split(b'\r\n',1)[0]
                checks['ERA HTTPS']={'status':int(first.split()[1]),'tls':True,'expected':https.get('expected_status',200)}
    except Exception:checks['ERA HTTPS']={'status':None,'tls':False,'expected':settings.get('https_check',{}).get('expected_status')}
    try:
        command(['getent','ahostsv4',settings['dns_check']],timeout=3);checks['host DNS']=True
    except Exception:checks['host DNS']=False
    return checks


def main():
    available={'fast':fast,'storage':storage,'smart':smart,'vms':vms,'services':services,
               'errors':errors,'backups':backups,'apps':apps}
    start=time.monotonic();cpu=time.process_time()
    result={}
    for name in sys.argv[1:] or ['fast']:
        if name not in available:continue
        before=time.monotonic()
        try:result[name]={'data':available[name](),'error':None}
        except Exception as e:result[name]={'data':None,'error':str(e)[:160]}
        result[name].update(checked_at=time.time(),duration_ms=round((time.monotonic()-before)*1000,2))
    children=resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({'groups':result,'probe_ms':round((time.monotonic()-start)*1000,2),
                      'cpu_ms':round((time.process_time()-cpu+children.ru_utime+children.ru_stime)*1000,2)}))


if __name__=='__main__':main()
