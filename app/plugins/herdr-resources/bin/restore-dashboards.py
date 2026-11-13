#!/usr/bin/env python3
"""Bounded startup retry. Reuse designated terminals; never touch busy ones.

Herdr App instance of the TUI startup restorer. The algorithm is preserved:
a single ``dashboards`` space, one uniquely labelled terminal per dashboard,
classify before acting (running / busy), a 60 s bounded retry under a lock,
JSON lines in startup.log, and NO focus changes anywhere.

Differences forced by the App model:
* workspace/pane IDs -> the ``dashboards`` space and ``herdr.plugin`` labels;
* ``herdr pane process-info`` -> the terminal record from ``hc ls --json``
  (argv + state); a live terminal running the dashboard is left alone, a live
  terminal running anything else is busy and is never typed into;
* ``herdr pane run`` into an idle shell -> ``hc run`` of a kept, adopted
  terminal (closing a pane in the App ends its terminal, so a missing
  dashboard is recreated instead of re-injected).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
_LIB = Path(os.environ.get("HERDR_APP_PLUGINS_ROOT", str(ROOT.parent / "lib")))
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import herdr_app

STATE = herdr_app.state_dir("herdr-dashboards")
SPACE_NAME = os.environ.get("HERDR_DASHBOARDS_SPACE", "dashboards")
MACHINE = os.environ.get("HERDR_DASHBOARDS_MACHINE") or None
PANELS = {
    "LLM Usage": ("herdr-quota", "herdr.quota"),
    "Container resources": ("herdr-resources", "herdr.resources"),
    "Dev server health": ("herdr-pve-health", "herdr.pve-health"),
}


def log(event, **fields):
    with (STATE / "startup.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "event": event, **fields}) + "\n")


def classify(address: str, dashboard: str) -> str:
    record = herdr_app.terminal_record(address)
    argv = [str(part) for part in record.get("argv") or []]
    if any(dashboard in part for part in argv):
        return "running"
    foreground = record.get("foreground") or {}
    if any(dashboard in str(part) for part in foreground.get("argv") or []):
        return "running"
    return "busy"


def restore_once() -> bool:
    space = herdr_app.find_space(SPACE_NAME, MACHINE)
    if space is None:
        herdr_app.ensure_space(SPACE_NAME, MACHINE)
        log("created_space", space=SPACE_NAME, machine=MACHINE)
        space = herdr_app.find_space(SPACE_NAME, MACHINE)
        if space is None:
            return False
    space_local = herdr_app.local_id(space["id"])
    complete = True
    for label, (directory, plugin) in PANELS.items():
        dashboard = str(ROOT / directory / "bin" / "dashboard.py")
        existing = herdr_app.find_by_label(
            herdr_app.PLUGIN_LABEL, plugin, **{herdr_app.SPACE_LABEL: space_local}
        )
        if existing is not None:
            address = str(existing.get("address") or "")
            state = classify(address, dashboard)
            if state == "running":
                continue
            log("skip_busy", terminal=address, label=label)
            continue
        argv = [herdr_app.python_executable(), dashboard]
        try:
            address = herdr_app.run_plugin_terminal(
                plugin,
                argv,
                name=directory,
                cwd=str(ROOT / directory),
                machine=MACHINE,
                space_name=SPACE_NAME,
            )
        except Exception as exc:
            log("launch_failed", plugin=plugin, reason=str(exc)[:200])
            complete = False
            continue
        log("launched", terminal=address, label=label)
        complete = False
    return complete


def main():
    global SPACE_NAME, MACHINE
    parser = argparse.ArgumentParser()
    parser.add_argument("--space", default=SPACE_NAME)
    parser.add_argument("--machine", default=MACHINE)
    args = parser.parse_args()
    SPACE_NAME = args.space
    MACHINE = args.machine or None
    STATE.mkdir(parents=True, exist_ok=True)
    lock = herdr_app.FileLock(STATE / "startup.lock")
    if not lock.acquire():
        return
    try:
        deadline = time.monotonic() + 60
        log("start")
        while time.monotonic() < deadline:
            try:
                if restore_once():
                    log("complete")
                    return
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                log("retry", reason=type(exc).__name__)
            time.sleep(2)
        log("timeout")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
