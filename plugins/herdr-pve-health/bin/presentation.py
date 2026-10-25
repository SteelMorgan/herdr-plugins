"""Terminal presentation only: no SSH, collection or dependencies."""
import re
import time
import unicodedata

ESC = '\x1b['
RESET = ESC + '0m'
BG = ESC + '48;2;24;24;37m'
TEXT = ESC + '38;2;205;214;244m'
DIM = ESC + '38;2;147;153;178m'
EDGE = ESC + '38;2;69;71;90m'
BLUE = ESC + '38;2;137;180;250m'
GREEN = ESC + '38;2;166;227;161m'
YELLOW = ESC + '38;2;249;226;175m'
RED = ESC + '38;2;243;139;168m'
BOLD = ESC + '1m'
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def clean(text):
    return ''.join(c for c in str(text) if not unicodedata.category(c).startswith('C'))


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1
               for c in ANSI.sub('', text))


def fit(text, width):
    """Clip/pad styled terminal cells without splitting an ANSI escape."""
    width = max(0, width)
    result = []; used = 0; pos = 0
    while pos < len(text):
        match = ANSI.match(text, pos)
        if match:
            result.append(match.group()); pos = match.end(); continue
        c = text[pos]; step = cell_width(c)
        if used + step > width:break
        result.append(c); used += step; pos += 1
    return ''.join(result) + ' ' * (width-used)


def paint(text, color=TEXT, bold=False):
    return color + (BOLD if bold else ESC+'22m') + clean(text) + ESC+'22m' + TEXT


def bar(value, width=12, color=BLUE):
    if value is None:return paint('─'*width, EDGE)
    used = max(1 if value > 0 else 0, round(max(0, min(100, value))*width/100))
    return paint('━'*used, color) + paint('─'*(width-used), EDGE)


def card(title, body, width, height, badge=''):
    inner = max(1, width-4)
    label = ' '+clean(title)+' '
    top = paint('╭─', EDGE)+paint(label, BLUE, True)
    if badge:
        space = max(0, width-cell_width(top)-cell_width(badge)-2)
        top += paint('─'*space, EDGE)+badge+paint('─╮', EDGE)
    else:top += paint('─'*max(0,width-cell_width(top)-1)+'╮', EDGE)
    rows = [fit(top,width)]
    for row in (body + ['']*height)[:max(0,height-2)]:
        rows.append(paint('│ ',EDGE)+fit(row,inner)+paint(' │',EDGE))
    rows.append(paint('╰'+'─'*max(0,width-2)+'╯', EDGE))
    return rows


def columns(cards, gap=2):
    return [(' '*gap).join(row) for row in zip(*cards)]


def overview(data, busy, width, height, d, now=None):
    now = time.time() if now is None else now
    groups = data.get('groups', {})
    def get(key):return groups.get(key,{}).get('data')
    def fresh(key):
        g=groups.get(key,{})
        return bool(g.get('received_at')) and not g.get('error') and now-g['received_at']<=d.INTERVALS[key]*2+15
    def age(stamp,at=now):
        value=d.age(stamp,at)
        return value[:-1]+{'s':'с','m':'м','h':'ч'}.get(value[-1],value[-1]) if value and value[-1] in 'smh' else value
    def aged(key):return age(groups.get(key,{}).get('received_at'))
    def flag(ok, key):
        if not fresh(key) or ok is None:return paint('● НЕТ ДАННЫХ',YELLOW)
        return paint('● OK',GREEN) if ok else paint('● СБОЙ',RED)
    fast=get('fast') or {}; rates=data.get('rates',{}); mem=fast.get('mem',{})
    live=fresh('fast') and not data.get('ssh_error')
    storage=get('storage') or {}; disks=get('smart') or []; vms=get('vms') or []
    svc=get('services') or {}; apps=get('apps') or {}; agents=get('agents') or []
    backup=get('backups') or {}; archives=backup.get('vms',[]); win=data.get('windows_backup') or {}
    errors=(get('errors') or {}).get('counts',{})
    issues=[]
    critical=False
    if not live:issues.append('нет свежего соединения с сервером')
    for name,g in groups.items():
        if g.get('error'):issues.append('опрос '+name+': ошибка')
    for disk in disks:
        s=d.smart_values(disk)
        bad=s['passed'] is False or s['attribute_failures'] or s['write_protected']
        if bad:issues.append(clean(disk['device'])+': SMART СБОЙ');critical=True
        elif any(s[k] for k in ['realloc_events','pending','uncorrectable','crc','media_errors']):
            issues.append(clean(disk['device'])+': ошибки диска')
    for fs in storage.get('filesystems',[]):
        if fs['readonly'] or fs['percent']>=95:
            issues.append(clean(fs['mount'])+': '+('ТОЛЬКО ЧТЕНИЕ' if fs['readonly'] else 'диск заполнен'));critical=True
        elif fs['percent']>=85:issues.append(clean(fs['mount'])+': мало места')
    failed=svc.get('failed',[])+[x.get('Id','?') for x in svc.get('units',[]) if x.get('ActiveState')!='active']
    if failed:issues.append('службы: '+', '.join(sorted(set(clean(x) for x in failed))))
    if any(errors.values()) and fresh('errors'):issues.append('ошибки ядра за 30 мин');critical=True
    if any(not x.get('completed_at') or now-x['completed_at']>36*3600 or not x.get('identity_matches') for x in archives):
        issues.append('проверить бекапы VM')
    # Summary must reflect failed/stale groups even when the host itself is live.
    missing=[k for k in d.INTERVALS if not fresh(k)]
    if missing:issues.append('нет свежих данных: '+', '.join(missing))
    for vm in vms:
        if vm.get('status')!='running':issues.append('VM '+str(vm.get('vmid'))+': остановлена')
    http=apps.get('ERA HTTPS',{})
    checks=[('1С',apps.get('1C TCP'),'apps'),('PostgreSQL',(get('onec') or {}).get('postgres_tcp'),'onec'),
            ('ERA',bool(http.get('tls')) and http.get('status')==http.get('expected'),'apps'),
            ('DNS',apps.get('host DNS'),'apps'),
            ('Hermes',all(x.get('ActiveState')=='active' for x in agents) if agents else None,'agents'),
            ('VPN',bool((get('vpn') or {}).get('interfaces')),'vpn')]
    for name,ok,key in checks:
        if fresh(key) and ok is False:issues.append(name+': сбой проверки');critical=True
    same=bool(backup.get('published_at')) and win.get('ServerGeneratedAt')==backup['published_at'] and win.get('Status')=='success'
    if fresh('backups') and not same:issues.append('Windows: нет копии актуального экспорта')
    jobs=backup.get('jobs',[]); enabled=sum(bool(x.get('enabled',True)) for x in jobs)
    if fresh('backups') and (not enabled or backup.get('jobs_error')):issues.append('расписание бекапов: проверить')
    health='ЕСТЬ СБОЙ' if critical else 'ВНИМАНИЕ' if issues else 'В НОРМЕ'
    color=RED if critical else YELLOW if issues else GREEN
    title=paint(' '+fast.get('hostname','DEV SERVER'),TEXT,True)+paint('  /  СОСТОЯНИЕ СЕРВЕРА',DIM)
    right=paint('● '+health,color,True)+paint('  обновлено '+aged('fast')+' назад ',DIM)
    rows=[fit(title,max(0,width-cell_width(right)))+right]
    message=' · '.join(issues) if issues else 'Проверки пройдены · диски, виртуальные машины и приложения доступны'
    if cell_width(message)>width-4:message=fit(message,width-7).rstrip()+'…'
    rows.append(paint(' '+('! ' if issues else '✓ ')+message,color))

    total=mem.get('MemTotal'); used=total-mem.get('MemAvailable',total) if total else None
    ram=100*used/total if total else None
    cpu=rates.get('cpu') if live else None
    temp=max((x['value'] for x in fast.get('sensors',[]) if x['label'].startswith(('Core','Package'))),default=None)
    kpis=[
        ('ПРОЦЕССОР',[paint(d.n(cpu)+' %',TEXT,True)+'  '+bar(cpu,10),
                        paint('ожидание I/O '+d.n(rates.get('wait'))+' %',DIM),
                        paint('load '+('/'.join(d.n(x) for x in fast.get('load',[])) or '—'),DIM)]),
        ('ПАМЯТЬ',[paint(d.n(ram)+' %',TEXT,True)+'  '+bar(ram,10,YELLOW if ram and ram>=85 else BLUE),
                    paint(d.size(used)+' / '+d.size(total),DIM),
                    paint('swap '+d.size(mem.get('SwapTotal',0)-mem.get('SwapFree',0)) if total else 'нет данных',DIM)]),
        ('СЕТЬ',[paint('↓ '+d.size(rates.get('net_rx'))+'/с',TEXT,True),
                  paint('↑ '+d.size(rates.get('net_tx'))+'/с',TEXT,True),
                  paint('ошибки '+str(sum(x['errors'] for x in fast.get('net',{}).values()))+' · физ. интерфейсы',DIM)]),
        ('ТЕМПЕРАТУРА',[paint(d.n(temp,0)+' °C',TEXT,True),paint('максимум ядер CPU',DIM),
                        paint('uptime '+d.n(fast['uptime']/3600 if 'uptime' in fast else None)+' ч',DIM)])]
    if width<108:
        # On a narrow terminal retain hierarchy, stack the four detail cards.
        ncols=2;kw=(width-2)//2
        for i in range(0,4,2):rows+=columns([card(t,b,kw,5) for t,b in kpis[i:i+2]])
    else:
        kw=(width-6)//4;widths=[kw]*3+[width-6-kw*3]
        rows+=columns([card(t,b,w,5) for (t,b),w in zip(kpis,widths)])
    rows+=['']

    disk_rows=[]
    for disk in disks[:2]:
        s=d.smart_values(disk);io=rates.get('disk',{}).get(disk['device'],{})
        ok=s['passed'] if not s['attribute_failures'] and not s['write_protected'] else False
        disk_rows.append(paint(disk['device'],TEXT,True)+'  '+flag(ok,'smart')+'  '+paint(d.n(s['temperature'],0)+' °C',DIM))
        disk_rows.append(paint(' чт/зап '+d.size(io.get('read'))+'/'+d.size(io.get('write'))+'/с · '+d.n(io.get('await_ms'))+' мс',DIM))
    if not disks:disk_rows=[paint('SMART: нет свежих данных',YELLOW)]
    for fs in storage.get('filesystems',[])[:2]:
        disk_rows.append(paint(fs['mount'].ljust(8),TEXT)+bar(fs['percent'],10,RED if fs['readonly'] or fs['percent']>=95 else YELLOW if fs['percent']>=85 else BLUE)
                         +paint(' '+d.n(fs['percent'],0)+'% · '+d.size(fs['free'])+' своб.',DIM))
    vm_rows=[paint(' ID   ИМЯ              CPU        RAM хоста',DIM)]
    for vm in sorted(vms,key=lambda x:x.get('vmid',0))[:5]:
        ok=vm.get('status')=='running'
        vm_rows.append(paint('● ',GREEN if ok and fresh('vms') else YELLOW)
                       +paint(f"{vm['vmid']:<4} {vm.get('name',''):<16.16}")
                       +paint(f"{d.n(vm.get('cpu')*100 if vm.get('cpu') is not None else None):>5}%  ")
                       +paint(d.size(vm.get('mem')) if ok else 'STOP',DIM if ok else YELLOW))
    if not vms:vm_rows.append(paint('Ожидание списка VM…',YELLOW))

    service_rows=[paint('1С TCP       ')+flag(apps.get('1C TCP'),'apps')+paint('   PG TCP ')+flag((get('onec') or {}).get('postgres_tcp'),'onec'),
                  paint('Hermes       ')+flag(checks[4][1],'agents')+paint('   VPN    ')+flag(checks[5][1],'vpn'),
                  paint('ERA HTTPS    ')+flag(checks[2][1],'apps')+paint('   DNS    ')+flag(apps.get('host DNS'),'apps'),
                  paint('Ядро: I/O '+str(errors.get('io','—'))+' · OOM '+str(errors.get('oom','—'))+' · reset '+str(errors.get('reset','—'))+'  / 30 мин',RED if any(errors.values()) else DIM)]
    vm_age='VM '+ ' · '.join(str(x['vmid']) for x in archives) + '  / возраст — в деталях'
    oldest=min((x['completed_at'] for x in archives if x.get('completed_at')),default=None)
    all_backups=bool(archives) and all(x.get('completed_at') and now-x['completed_at']<=36*3600 and x.get('identity_matches') for x in archives)
    completed=win.get('CompletedAt','')
    backup_rows=[paint('VM: '+age(oldest)+' назад  ')+flag(all_backups,'backups')+paint('  предел 36 ч',DIM),
                 paint(vm_age or 'Архивы VM: нет данных',DIM),
                 paint('Windows D: ')+paint('● СКОПИРОВАНО' if same else '● ПРОВЕРИТЬ',GREEN if same and fresh('backups') else YELLOW)
                 +paint('  '+(completed[8:10]+'.'+completed[5:7]+' '+completed[11:16] if completed else '—')+' · '+str(win.get('VerifiedFiles','—'))+'/6',DIM),
                 paint('Хост '+age(backup.get('host_completed_at'))+' · задания '+str(enabled)+'/'+str(len(jobs))+' · SHA при создании',DIM)]
    lw=(width-2)//2;rw=width-2-lw
    sections=[('ДИСКИ И МЕСТО',disk_rows,'SMART '+aged('smart')),
              ('ВИРТУАЛЬНЫЕ МАШИНЫ',vm_rows,str(sum(v.get('status')=='running' for v in vms))+'/'+str(len(vms))+' RUN'),
              ('ПРИЛОЖЕНИЯ И СВЯЗЬ',service_rows,'5 мин'),
              ('РЕЗЕРВНЫЕ КОПИИ',backup_rows,'5 мин')]
    if width>=108:
        rows+=columns([card(t,b,w,8,paint(' '+badge+' ',DIM)) for (t,b,badge),w in zip(sections[:2],[lw,rw])])
        rows+=['']
        rows+=columns([card(t,b,w,6,paint(' '+badge+' ',DIM)) for (t,b,badge),w in zip(sections[2:],[lw,rw])])
    else:
        for t,b,badge in sections:rows+=card(t,b,width,len(b)+2,paint(' '+badge+' ',DIM))
    footer=paint(' d детали  ·  r обновить  ',BLUE)+paint('опрос: 30с / VM 2м / SMART 15м'+('  · сбор…' if busy else ''),DIM)
    limit=max(1,height-1)
    if len(rows)>limit:
        rows=rows[:max(0,limit-1)]+[paint(' Экран мал: d — все детали; increase pane height',YELLOW)]
    rows=rows[:limit]+['']*max(0,limit-len(rows))
    return ESC+'H'+'\r\n'.join(BG+fit(r,width)+RESET for r in rows+[footer])+ESC+'J'
