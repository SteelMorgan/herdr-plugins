#!/usr/bin/env python3
"""Refresh provider console cookies (MIMO / Qwen) — native orchestrator.

Herdr App replacement for the TUI's WSL bridge scripts refresh-mimo-cookies.sh
and refresh-qwen-cookies.sh. The algorithm is unchanged:

1. run the Windows-native CDP extractor (mimo_cookie_refresh.py /
   qwen_cookie_refresh.py) against the persistent Chrome/Brave profile;
2. read the cookie header it writes under %TEMP%\\herdr-quota-cookies;
3. validate the session against the provider API;
4. atomically store .secrets/<provider>-web-session.json with mode 0600.

On Windows everything runs natively. Under WSL the extractor still runs
through Windows interop (python.exe), exactly like the old bridge; the
validation/storage step now runs in this same Python instead of a heredoc.

Usage:
    refresh_cookies.py mimo [--auto]
    refresh_cookies.py qwen
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def _windows_python() -> str | None:
    """Windows interpreter for the CDP extractor (native or WSL interop)."""
    if IS_WINDOWS:
        return sys.executable
    candidate = Path("/mnt/c/Users") / Path.home().name / "AppData/Local/Programs/Python/Python312/python.exe"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("python.exe")


def _temp_dir() -> Path:
    if IS_WINDOWS:
        return Path(tempfile.gettempdir()) / "herdr-quota-cookies"
    # WSL: the Windows user's %TEMP%.
    return Path("/mnt/c/Users") / Path.home().name / "AppData/Local/Temp/herdr-quota-cookies"


def _run_extractor(script: Path, auto: bool) -> None:
    python = _windows_python()
    if not python:
        raise RuntimeError("Windows python.exe not found (CDP extraction runs on the Windows host)")
    env = dict(os.environ)
    if auto:
        env["MIMO_AUTO"] = "1"
    if IS_WINDOWS:
        command = [python, str(script)]
        cwd = None
    else:
        # Windows processes cannot start with a UNC working directory.
        result = subprocess.run(["wslpath", "-w", str(script)], capture_output=True, text=True, check=True, creationflags=NO_WINDOW)
        command = [python, result.stdout.strip()]
        cwd = "/mnt/c"
    completed = subprocess.run(command, cwd=cwd, env=env, check=False, creationflags=NO_WINDOW)
    if completed.returncode != 0:
        if auto:
            raise RuntimeError("silent refresh failed — run without --auto and log in")
        raise RuntimeError("extraction failed (log in in the opened browser window, then re-run)")


def _read_cookie(env_name: str, file_name: str) -> str:
    path = _temp_dir() / file_name
    line = path.read_text(encoding="utf-8").strip()
    return json.loads(line.split(f"{env_name}=", 1)[1])


def _save_secret(target_name: str, payload: dict) -> Path:
    secret_dir = PLUGIN_ROOT / ".secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(secret_dir, 0o700)
    except OSError:
        pass
    target = secret_dir / target_name
    fd, tmp_name = tempfile.mkstemp(dir=str(secret_dir), prefix=f".{target_name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, target)
    return target


def refresh_mimo(auto: bool) -> int:
    _run_extractor(PLUGIN_ROOT / "bin" / "mimo_cookie_refresh.py", auto)
    cookie = _read_cookie("MIMO_WEB_COOKIES", "mimo-cookies.env")
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
        return 1
    if not isinstance(payload, dict) or payload.get("code") == 401:
        print("herdr-quota: cookies captured but session is expired — log in in the browser and re-run", file=sys.stderr)
        return 1
    names = [x.split("=", 1)[0].strip() for x in cookie.split(";") if x.strip()]
    profile = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "herdr-quota-chrome"
        if IS_WINDOWS
        else Path("/mnt/c/Users") / Path.home().name / "AppData/Local/herdr-quota-chrome"
    )
    target = _save_secret(
        "mimo-web-session.json",
        {
            "provider": "mimo-web",
            "cookie": cookie,
            "captured_at": datetime.now().astimezone().isoformat(),
            "cookie_names": names,
            "source": "cdp-profile-auto" if auto else "cdp-profile",
            "profile": str(profile),
        },
    )
    print(f"herdr-quota: MIMO session saved to {target}; session valid")
    return 0


def refresh_qwen() -> int:
    _run_extractor(PLUGIN_ROOT / "bin" / "qwen_cookie_refresh.py", auto=False)
    cookie = _read_cookie("QWENCLOUD_COOKIES", "qwencloud-cookies.env")
    try:
        req = urllib.request.Request(
            "https://home.qwencloud.com/tool/user/info.json",
            headers={"Cookie": cookie, "User-Agent": "herdr-quota/0.1.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            info = json.load(response)
    except Exception as exc:
        print(f"herdr-quota: session check failed: {exc}", file=sys.stderr)
        return 1
    sec = (info.get("data") or {}).get("secToken")
    if info.get("code") == "ConsoleNeedLogin" or not sec or sec == "0":
        print("herdr-quota: cookies captured but session is expired — log in in the Brave window and re-run", file=sys.stderr)
        return 1
    target = _save_secret(
        "qwen-web-session.json",
        {
            "provider": "qwen-web",
            "origin": "https://home.qwencloud.com",
            "cookie": cookie,
            "updated_at": datetime.now().astimezone().isoformat(),
        },
    )
    print(f"herdr-quota: Qwen session saved to {target}; session valid")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("mimo", "qwen"):
        print("usage: refresh_cookies.py {mimo [--auto] | qwen}", file=sys.stderr)
        return 2
    auto = "--auto" in argv[2:]
    try:
        if argv[1] == "mimo":
            return refresh_mimo(auto)
        return refresh_qwen()
    except Exception as exc:
        print(f"herdr-quota: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
