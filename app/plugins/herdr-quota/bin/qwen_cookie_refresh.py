"""Refresh QWENCLOUD_COOKIES from a dedicated Brave profile via CDP.

Runs ON WINDOWS (through WSL interop). Uses its own Brave profile so the
main browser is never touched. Flow:
  1. If the debug port is closed, launch Brave with that profile + port.
  2. Wait for the port, then read cookies through the browser itself
     (works around Chromium v20 app-bound cookie encryption).
  3. Write cookies to %TEMP%\\herdr-quota-cookies\\qwencloud-cookies.env.

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

CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "herdr-quota-brave")
LOGIN_URL = "https://home.qwencloud.com/analytics/token-plan/team"
OUT_DIR = os.path.join(tempfile.gettempdir(), "herdr-quota-cookies")
OUT_FILE = os.path.join(OUT_DIR, "qwencloud-cookies.env")
DOMAIN_SUFFIXES = ("qwencloud.com",)

BRAVE_CANDIDATES = [
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


def launch_brave() -> None:
    brave = next((p for p in BRAVE_CANDIDATES if os.path.isfile(p)), None)
    if not brave:
        raise RuntimeError("brave.exe not found")
    subprocess.Popen(
        [brave, f"--user-data-dir={PROFILE_DIR}", f"--remote-debugging-port={CDP_PORT}",
         "--no-first-run", "--no-default-browser-check", LOGIN_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def browser_ws_url() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5) as resp:
        return json.load(resp)["webSocketDebuggerUrl"]


def domain_matches(domain: str) -> bool:
    d = domain.lstrip(".")
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
    if not selected:
        print(f"NO_COOKIES qwencloud cookies not found; log in at {LOGIN_URL} and re-run")
        return 1

    header = "; ".join(f"{name}={value}" for _, name, value in selected)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("QWENCLOUD_COOKIES=" + json.dumps(header) + "\n")
    names = sorted({f"{domain}:{name}" for domain, name, _ in selected})
    print(f"OK cookies={len(selected)} bytes={len(header)} out={OUT_FILE}")
    print("names=" + ", ".join(names))
    return 0


def main() -> int:
    if not port_open():
        launch_brave()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not port_open():
            time.sleep(0.5)
        if not port_open():
            print("ERROR debug port did not come up")
            return 2
    return asyncio.run(extract())


if __name__ == "__main__":
    sys.exit(main())
