#!/usr/bin/env python3
"""Remote side of the Herdr clipboard bridge. Uses Xvfb :99 + xclip."""
import base64
import hashlib
import json
import os
import subprocess
import sys

ENV = dict(os.environ, DISPLAY=':99')


def xclip(*args, inp=None):
    return subprocess.run(['xclip', '-selection', 'clipboard', *args],
                          input=inp, capture_output=True, env=ENV)


cmd = sys.argv[1] if len(sys.argv) > 1 else 'get'
if cmd == 'get':
    tg = xclip('-t', 'TARGETS', '-o')
    targets = tg.stdout.decode('utf-8', 'replace').split() if tg.returncode == 0 else []
    has_text = any(x in targets for x in ('UTF8_STRING', 'TEXT', 'STRING'))
    text = ''
    if has_text:
        t = xclip('-o')
        text = t.stdout.decode('utf-8', 'replace') if t.returncode == 0 else ''
    i = xclip('-t', 'image/png', '-o')
    ih = hashlib.sha256(i.stdout).hexdigest() if i.returncode == 0 and i.stdout else ''
    print(json.dumps({'text': text, 'img': ih}))
elif cmd == 'get-image':
    i = xclip('-t', 'image/png', '-o')
    sys.stdout.write(base64.b64encode(i.stdout).decode() if i.returncode == 0 else '')
elif cmd in ('set-text', 'set-image'):
    # xclip -i forks and keeps inherited fds open; feed it a temp file and
    # detach stdout/stderr so the ssh channel closes immediately.
    import tempfile
    data = sys.stdin.buffer.read()
    if cmd == 'set-image':
        data = base64.b64decode(data)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmp = f.name
    args = ['-i'] if cmd == 'set-text' else ['-t', 'image/png', '-i']
    with open(tmp, 'rb') as fin:
        subprocess.run(['xclip', '-selection', 'clipboard', *args],
                       stdin=fin, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=ENV)
    os.unlink(tmp)
