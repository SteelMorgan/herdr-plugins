#!/usr/bin/env python3
"""MiMo CLI actions for the Herdr App (TUI plugin herdr.mimo, adapted).

Behaviour preserved from the TUI shell scripts:
* open-tui     — open or focus the MiMo terminal (matched by label, the TUI
                 matched the pane label "MiMo");
* start-here   — run mimo in the invoking terminal; refuses to take over a
                 terminal that hosts a recognized coding agent (falls back
                 to the MiMo pane);
* models / stats / llm-server-issue / llm-server-list — thin MiMo CLI calls;
* diagnose     — binary, version, App environment and CLI surface.

The invoking terminal context arrives as --terminal (the mod passes the
focused terminal) or $HC_TERMINAL_ID, replacing HERDR_PANE_ID.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_LIB = Path(os.environ.get("HERDR_APP_PLUGINS_ROOT", str(ROOT.parents[1]))) / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import herdr_app

PLUGIN_ID = "herdr.mimo"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def mimo_binary() -> str:
    configured = os.environ.get("MIMO_BIN")
    if configured:
        return configured
    home = Path.home()
    candidates = [home / ".mimocode" / "bin" / ("mimo.exe" if os.name == "nt" else "mimo")]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("mimo")
    if found:
        return found
    raise SystemExit(
        "error: mimo binary not found; set MIMO_BIN or install MiMoCode CLI"
    )


def invoking_terminal(args: argparse.Namespace) -> str | None:
    return args.terminal or os.environ.get("HC_TERMINAL_ID") or None


def terminal_record(address: str) -> dict:
    try:
        return herdr_app.hc("get", address, timeout=10)
    except Exception:
        return {}


def resolve_cwd(address: str | None) -> str:
    """Prefer the invoking terminal's live cwd, then stay put (TUI rule)."""
    if address:
        record = terminal_record(address)
        foreground = record.get("foreground") or {}
        cwd = foreground.get("cwd") or record.get("cwd") or ""
        if cwd and Path(cwd).is_dir():
            return str(cwd)
    return os.getcwd()


def terminal_has_agent(address: str) -> bool:
    record = terminal_record(address)
    status = record.get("status")
    return bool(isinstance(status, dict) and status.get("app"))


def open_tui(cwd: str) -> int:
    binary = mimo_binary()
    address = herdr_app.open_plugin_pane(
        PLUGIN_ID,
        [binary],
        name="herdr-mimo",
        cwd=cwd,
    )
    print(f"MiMo terminal: {address}")
    return 0


def start_here(address: str | None) -> int:
    binary = mimo_binary()
    if address:
        if terminal_has_agent(address):
            print(
                f"refusing: terminal {address} already hosts a coding agent; opening MiMo terminal instead",
                file=sys.stderr,
            )
            return open_tui(resolve_cwd(address))
        # The TUI injected the command with `herdr pane run`; typing it into
        # the shell is the App equivalent and keeps the shell alive.
        herdr_app.hc("send", address, binary, timeout=30)
        print(f"started MiMo in terminal {address}")
        return 0
    return open_tui(os.getcwd())


def run_mimo(*args: str) -> int:
    binary = mimo_binary()
    return subprocess.call([binary, *args], creationflags=NO_WINDOW)


def diagnose(address: str | None) -> int:
    print("herdr.mimo diagnose (Herdr App)")
    print("===============================")
    try:
        binary = mimo_binary()
    except SystemExit as exc:
        print(exc)
        return 1
    print(f"MIMO_BIN={binary}")
    if Path(binary).is_file() or shutil.which(binary):
        print("mimo_binary: ok")
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False, creationflags=NO_WINDOW)
        print("mimo_version: " + (version.stdout.strip() or version.stderr.strip() or "(no --version)"))
    else:
        print("mimo_binary: MISSING")
    print(f"hc_cli: {shutil.which('hc') or 'MISSING'}")
    print(f"herdr_app_cli: {shutil.which('herdr-app') or 'MISSING'}")
    print(f"HC_TERMINAL_ID={os.environ.get('HC_TERMINAL_ID', '')}")
    print(f"invoking_terminal={address or ''}")
    print(f"HERDR_APP_PLUGINS_ROOT={os.environ.get('HERDR_APP_PLUGINS_ROOT', '')}")
    print(f"HERDR_PLUGIN_CONFIG_DIR={os.environ.get('HERDR_PLUGIN_CONFIG_DIR', '')}")
    print(f"cwd={os.getcwd()}")
    print()
    print("mimo CLI surface:")
    for command in ("llm-server", "models", "stats", "agent", "run"):
        probe = subprocess.run(
            [binary, command, "--help"], capture_output=True, text=True, check=False, creationflags=NO_WINDOW
        )
        print(f"  {command}: {'ok' if probe.returncode == 0 else 'unavailable'}")
    print()
    print("llm-server tokens (this directory):")
    subprocess.call([binary, "llm-server", "list"], creationflags=NO_WINDOW)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="MiMo actions for the Herdr App")
    parser.add_argument(
        "action",
        choices=["open-tui", "start-here", "models", "stats", "llm-server-issue", "llm-server-list", "diagnose"],
    )
    parser.add_argument("--terminal", help="invoking terminal address (App context)")
    args = parser.parse_args(argv[1:])
    address = invoking_terminal(args)
    if args.action == "open-tui":
        return open_tui(resolve_cwd(address))
    if args.action == "start-here":
        return start_here(address)
    if args.action == "models":
        return run_mimo("models")
    if args.action == "stats":
        return run_mimo("stats")
    if args.action == "llm-server-issue":
        return run_mimo("llm-server", "issue")
    if args.action == "llm-server-list":
        return run_mimo("llm-server", "list")
    return diagnose(address)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
