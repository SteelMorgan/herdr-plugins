"""Refresh MIMO platform cookies from a dedicated Chrome/Brave profile via CDP.

Runs ON WINDOWS (through WSL interop). Two-phase login — Google OAuth drops
the connection when `--remote-debugging-port` is present ("гугл внезапно
разорвал соединение"), so the login window is a normal browser:

  1. LOGIN  — launch Chrome with the dedicated profile, NO debug port.
              User finishes Google/Xiaomi OAuth in that window.
  2. EXTRACT — after the profile is logged in, relaunch WITH debug port
              (no OAuth dance; SSO renews serviceToken) and read cookies
              through the browser itself (bypasses app-bound encryption).

Silent (`--auto`): extract only — for renewal while passToken is alive.

Prints cookie NAMES only; values never reach stdout.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import websockets

CDP_PORT = int(os.environ.get("CDP_PORT", "9223"))
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "herdr-quota-chrome")
LOGIN_URL = "https://platform.xiaomimimo.com/console/usage"
OUT_DIR = os.path.join(tempfile.gettempdir(), "herdr-quota-cookies")
OUT_FILE = os.path.join(OUT_DIR, "mimo-cookies.env")
DOMAIN_SUFFIXES = ("xiaomimimo.com", "xiaomi.com")

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"BraveSoftware\Brave-Browser\Application\brave.exe"),
]


def port_open() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def pick_browser() -> str:
    exe = next((p for p in BROWSER_CANDIDATES if os.path.isfile(p)), None)
    if not exe:
        raise RuntimeError("chrome.exe/brave.exe not found")
    return exe


def launch_browser(debug: bool) -> str:
    exe = pick_browser()
    args = [exe, f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check"]
    if debug:
        args.append(f"--remote-debugging-port={CDP_PORT}")
    args.append(LOGIN_URL)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return exe


def kill_profile_browser() -> None:
    """Stop only Chrome/Brave processes using our dedicated profile."""
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -like '*herdr-quota-chrome*' -and "
        "$_.Name -match 'chrome|brave|msedge' "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)


def browser_ws_url() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5) as resp:
        return json.load(resp)["webSocketDebuggerUrl"]


def domain_matches(domain: str) -> bool:
    d = (domain or "").lstrip(".")
    return any(d == s or d.endswith("." + s) for s in DOMAIN_SUFFIXES)


async def extract() -> int:
    async with websockets.connect(browser_ws_url(), max_size=2**26) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == 1:
                if "error" in message:
                    print(f"CDP_ERROR {message['error']}")
                    return 1
                cookies = message["result"]["cookies"]
                break

    selected = [(c["domain"], c["name"], c["value"]) for c in cookies if domain_matches(c.get("domain", ""))]
    platform = [(d, n, v) for d, n, v in selected if "xiaomimimo" in d]
    picked = platform or selected
    names = {n for _, n, _ in picked}
    if not picked or "api-platform_serviceToken" not in names:
        listed = sorted({n for _, n, _ in selected})
        print("NO_COOKIES api-platform_serviceToken not found; have=" + ", ".join(listed))
        return 1

    header = "; ".join(f"{name}={value}" for _, name, value in picked)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("MIMO_WEB_COOKIES=" + json.dumps(header) + "\n")
    listed = sorted({f"{domain}:{name}" for domain, name, _ in selected})
    print(f"OK cookies={len(picked)} bytes={len(header)} out={OUT_FILE}")
    print("names=" + ", ".join(listed))
    return 0


def extract_sync_once() -> bool:
    try:
        return asyncio.run(extract()) == 0
    except Exception:
        return False


def wait_port(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_open():
            return True
        time.sleep(0.4)
    return False


def run_extract() -> int:
    """Launch (or reuse) browser WITH debug port and extract cookies."""
    if not port_open():
        try:
            launch_browser(debug=True)
        except RuntimeError as exc:
            print(f"ERROR {exc}")
            return 3
        if not wait_port(30):
            print("ERROR debug port did not come up")
            return 2
        # Profile SSO needs a beat to mint serviceToken.
        time.sleep(5)
    return asyncio.run(extract())


def run_login() -> int:
    """Phase 1: normal browser (Google OAuth is fine). Phase 2: CDP extract."""
    if port_open():
        # Already has debug port (old instance) — still extract, but prefer
        # a clean no-debug login if the user is retrying after a Google drop.
        pass

    print("LOGIN WINDOW — no debug port (Google OAuth accepts this).")
    print("Sign in with Google / Xiaomi at the console. Wait until Used / Limit appear.")
    kill_profile_browser()
    try:
        launch_browser(debug=False)
    except RuntimeError as exc:
        print(f"ERROR {exc}")
        return 3

    # Poll the cookie jar is impossible without CDP; wait on the human.
    try:
        input("Press Enter here when the console shows quota numbers … ")
    except EOFError:
        pass

    print("Closing login browser …")
    kill_profile_browser()
    time.sleep(1)
    print("EXTRACT via CDP (profile is already signed in; no OAuth here) …")
    return run_extract()


def main() -> int:
    auto = "--auto" in sys.argv
    login = "--login" in sys.argv or not auto
    if auto:
        return run_extract()
    return run_login() if login else run_extract()


if __name__ == "__main__":
    sys.exit(main())
