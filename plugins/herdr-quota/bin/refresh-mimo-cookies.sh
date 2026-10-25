#!/bin/sh
# Refresh the platform.xiaomimimo.com session cookie for the MIMO provider.
# Opens Chrome/Brave (dedicated profile + remote-debugging port) on the Windows
# host — a real browser, so Google OAuth accepts it (Playwright is not used).
# Reads cookies through the browser itself (bypasses app-bound encryption),
# validates the session, and stores the result in .secrets with mode 0600.
#
#   refresh-mimo-cookies.sh          # interactive: log in once in the opened window
#   refresh-mimo-cookies.sh --auto   # silent: renew from the existing profile
set -e

PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
REFRESH_PY="$PLUGIN_ROOT/bin/mimo_cookie_refresh.py"
REFRESH_WIN=$(wslpath -w "$REFRESH_PY")
AUTO_FLAG=${1:-}
if [ "$AUTO_FLAG" = "--auto" ]; then
    export MIMO_AUTO=1
fi
# Interactive login is the default (two-phase: OAuth without CDP, then extract).

PYTHON_EXE="/mnt/c/Users/steel/AppData/Local/Programs/Python/Python312/python.exe"
[ -x "$PYTHON_EXE" ] || PYTHON_EXE=$(command -v python.exe || true)
if [ -z "$PYTHON_EXE" ]; then
    echo "herdr-quota: Windows python.exe not found" >&2
    exit 1
fi

# Windows processes cannot start with a UNC working directory, so run from C:.
cd /mnt/c
if ! "$PYTHON_EXE" "$REFRESH_WIN" $AUTO_FLAG; then
    if [ -n "$AUTO_FLAG" ]; then
        echo "herdr-quota: silent refresh failed — run without --auto and log in" >&2
    else
        echo "herdr-quota: extraction failed (log in in the opened browser window, then re-run)" >&2
    fi
    exit 1
fi

HERDR_PLUGIN_ROOT="$PLUGIN_ROOT" python3 - <<'EOF'
import json, os, sys, tempfile, time, urllib.request
from datetime import datetime
from pathlib import Path

plugin_root = Path(os.environ["HERDR_PLUGIN_ROOT"])
user = Path.home().name
tmp = Path(f"/mnt/c/Users/{user}/AppData/Local/Temp/herdr-quota-cookies/mimo-cookies.env")
line = tmp.read_text(encoding="utf-8").strip()
cookie = json.loads(line.split("MIMO_WEB_COOKIES=", 1)[1])

# Validate the session actually works before saving.
try:
    req = urllib.request.Request(
        "https://platform.xiaomimimo.com/api/v1/tokenPlan/usage",
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) herdr-quota/0.1.0",
            "Accept": "application/json",
            "Referer": "https://platform.xiaomimimo.com/console/usage",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        payload = json.load(response)
except Exception as exc:
    print(f"herdr-quota: session check failed: {exc}", file=sys.stderr)
    sys.exit(1)

if not isinstance(payload, dict) or payload.get("code") == 401:
    print("herdr-quota: cookies captured but session is expired — log in in the browser and re-run", file=sys.stderr)
    sys.exit(1)

names = [x.split("=", 1)[0].strip() for x in cookie.split(";") if x.strip()]
secret_dir = plugin_root / ".secrets"
secret_dir.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(secret_dir, 0o700)
except OSError:
    pass
target = secret_dir / "mimo-web-session.json"
fd, tmp_name = tempfile.mkstemp(dir=str(secret_dir), prefix=".mimo-web-session.", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "provider": "mimo-web",
            "cookie": cookie,
            "captured_at": datetime.now().astimezone().isoformat(),
            "cookie_names": names,
            "source": "cdp-profile-auto" if os.environ.get("MIMO_AUTO") else "cdp-profile",
            "profile": str(Path.home() / "AppData/Local/herdr-quota-chrome"),
        },
        handle,
        indent=2,
    )
    handle.write("\n")
os.chmod(tmp_name, 0o600)
os.replace(tmp_name, target)
print(f"herdr-quota: MIMO session saved to {target}; session valid")
EOF
