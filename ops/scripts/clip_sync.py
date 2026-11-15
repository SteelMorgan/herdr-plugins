"""Windows side of the Herdr clipboard bridge: mirror clipboard (text+image)
between the local Windows clipboard and remote machines' X clipboard (Xvfb+xclip).

Loop prevention: per-side last-seen state; a side that changed since last poll
wins and is propagated. Poll ~1.2s; ssh multiplexed via ControlMaster.
"""
import base64
import json
import os
import subprocess
import sys
import time

MACHINES = sys.argv[1:] or ['dev', '1c']
TEMP = os.environ.get('TEMP', r'C:\Windows\Temp')
LOCAL_PNG = os.path.join(TEMP, 'clipsync_local.png')
REMOTE_PNG = os.path.join(TEMP, 'clipsync_remote.png')
CM = os.path.join(os.environ['USERPROFILE'], '.ssh', 'cm-%r@%h-%p')

PS_GET = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$txt = [System.Windows.Forms.Clipboard]::GetText()
$ih = ''
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($img) { $img.Save('PNGPATH', [System.Drawing.Imaging.ImageFormat]::Png); $ih = (Get-FileHash 'PNGPATH').Hash }
@{ text = $txt; img = $ih } | ConvertTo-Json -Compress
'''.replace('PNGPATH', LOCAL_PNG.replace('\\', '\\\\'))


PS_UTF8 = ('[Console]::InputEncoding=[System.Text.Encoding]::UTF8; '
           '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; ')


def ps(script, inp=None):
    return subprocess.run(['powershell', '-sta', '-NoProfile', '-Command', PS_UTF8 + script],
                          input=inp, capture_output=True, text=True,
                          encoding='utf-8', errors='replace',
                          creationflags=0x08000000)


def local_get():
    r = ps(PS_GET)
    try:
        d = json.loads(r.stdout.strip() or '{}')
    except ValueError:
        return {'text': '', 'img': ''}
    d['text'] = (d.get('text') or '').replace('\r\n', '\n')
    return d


def local_set_text(t):
    ps('Add-Type -AssemblyName System.Windows.Forms; '
       '[System.Windows.Forms.Clipboard]::SetText([Console]::In.ReadToEnd())', inp=t)


def local_set_image(b64):
    with open(REMOTE_PNG, 'wb') as f:
        f.write(base64.b64decode(b64))
    ps(r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ms = New-Object System.IO.MemoryStream(,[System.IO.File]::ReadAllBytes('RPATH'))
$img = [System.Drawing.Image]::FromStream($ms)
[System.Windows.Forms.Clipboard]::SetDataObject($img, $true)
'''.replace('RPATH', REMOTE_PNG.replace('\\', '\\\\')))


def ssh(m, *args, inp=None):
    # Win32-OpenSSH has no ControlMaster; plain ssh per call (~300ms).
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                           {'dev': 'gbig-herdr-dev', '1c': 'gbig-1c-app'}[m],
                           'python3', '~/.local/bin/clip-bridge', *args],
                          input=inp, capture_output=True, text=True,
                          encoding='utf-8', errors='replace',
                          creationflags=0x08000000)


def remote_get(m):
    r = ssh(m, 'get')
    if r.returncode != 0 or not r.stdout.strip():
        return None  # machine unreachable: skip, never wipe local state
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None
    return {'text': (d.get('text') or '').replace('\r\n', '\n'),
            'img': d.get('img') or ''}


def main():
    prev_l = local_get()
    prev_r = {m: remote_get(m) for m in MACHINES}
    while True:
        time.sleep(2.0)
        try:
            cur_l = local_get()
            if cur_l != prev_l:
                print('L changed -> push', {k: (v[:30] if isinstance(v, str) else v) for k, v in cur_l.items()}, flush=True)
                for m in MACHINES:
                    ssh(m, 'set-text', inp=cur_l.get('text') or '')
                    if cur_l.get('img'):
                        with open(LOCAL_PNG, 'rb') as f:
                            ssh(m, 'set-image', inp=base64.b64encode(f.read()).decode())
                    prev_r[m] = dict(cur_l)
                prev_l = cur_l
                continue
            for m in MACHINES:
                cur_r = remote_get(m)
                if cur_r is None:
                    continue
                if prev_r[m] is None:
                    prev_r[m] = cur_r  # first contact: adopt, don't overwrite
                    continue
                if cur_r != prev_r[m]:
                    print(m, 'R changed -> pull', {k: (v[:30] if isinstance(v, str) else v) for k, v in cur_r.items()}, flush=True)
                    local_set_text(cur_r.get('text') or '')
                    if cur_r.get('img'):
                        b64 = ssh(m, 'get-image').stdout.strip()
                        if b64:
                            local_set_image(b64)
                    prev_l = dict(cur_r)
                    prev_r = {k: dict(cur_r) for k in MACHINES}
                    break
        except Exception as e:
            print('sync error:', e, file=sys.stderr)
            time.sleep(3)


if __name__ == '__main__':
    main()
