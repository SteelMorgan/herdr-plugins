#!/bin/sh
# Refresh the qwencloud.com session cookie for the QWEN provider.
# Opens a dedicated Brave window (own profile + remote-debugging port) on the
# Windows host, reads the cookies through the browser itself (which bypasses
# Chromium's app-bound cookie encryption), validates the session, and stores
# the result in the plugin's .secrets directory with mode 0600.
set -e

PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
REFRESH_PY="$PLUGIN_ROOT/bin/qwen_cookie_refresh.py"
REFRESH_WIN=$(wslpath -w "$REFRESH_PY")

PYTHON_EXE="/mnt/c/Users/steel/AppData/Local/Programs/Python/Python312/python.exe"
[ -x "$PYTHON_EXE" ] || PYTHON_EXE=$(command -v python.exe || true)
if [ -z "$PYTHON_EXE" ]; then
    echo "herdr-quota: Windows python.exe not found" >&2
    exit 1
fi

# Windows processes cannot start with a UNC working directory, so run from C:.
cd /mnt/c
if ! "$PYTHON_EXE" "$REFRESH_WIN"; then
    echo "herdr-quota: extraction failed (log in in the opened Brave window, then re-run)" >&2
    exit 1
fi

HERDR_PLUGIN_ROOT="$PLUGIN_ROOT" python3 - <<'EOF'
import json, os, sys, tempfile, time, urllib.request
from datetime import datetime
from pathlib import Path

plugin_root = Path(os.environ["HERDR_PLUGIN_ROOT"])
user = Path.home().name
tmp = Path(f"/mnt/c/Users/{user}/AppData/Local/Temp/herdr-quota-cookies/qwencloud-cookies.env")
line = tmp.read_text(encoding="utf-8").strip()
cookie = json.loads(line.split("QWENCLOUD_COOKIES=", 1)[1])

try:
    req = urllib.request.Request(
        "https://home.qwencloud.com/tool/user/info.json",
        headers={"Cookie": cookie, "User-Agent": "herdr-quota/0.1.0"},
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        info = json.load(response)
except Exception as exc:
    print(f"herdr-quota: session check failed: {exc}", file=sys.stderr)
    sys.exit(1)

sec = (info.get("data") or {}).get("secToken")
if info.get("code") == "ConsoleNeedLogin" or not sec or sec == "0":
    print("herdr-quota: cookies captured but session is expired — log in in the Brave window and re-run", file=sys.stderr)
    sys.exit(1)

secret_dir = plugin_root / ".secrets"
secret_dir.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(secret_dir, 0o700)
except OSError:
    pass
target = secret_dir / "qwen-web-session.json"
fd, tmp_name = tempfile.mkstemp(dir=str(secret_dir), prefix=".qwen-web-session.", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "provider": "qwen-web",
            "origin": "https://home.qwencloud.com",
            "cookie": cookie,
            "updated_at": datetime.now().astimezone().isoformat(),
        },
        handle,
        indent=2,
    )
    handle.write("\n")
os.chmod(tmp_name, 0o600)
os.replace(tmp_name, target)
print(f"herdr-quota: Qwen session saved to {target}; session valid")
EOF
