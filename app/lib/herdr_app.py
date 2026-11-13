#!/usr/bin/env python3
"""Shared Herdr App integration layer for the app plugin instance.

This is the App-side replacement for the TUI plugin runtime. The TUI plugins
talked to the ``herdr`` CLI and its UNIX socket (plugin panes, agent.list,
pane.read, events.subscribe). The Herdr App has no plugin v1 runtime: its
equivalents are

* ``hc`` (herdr-core) for terminals: ``ls``, ``read``, ``send``, ``run``,
  ``label``, ``events`` — local and on managed machines (``machine/tN``);
* ``herdr-app do`` for what the person sees: spaces, tabs, panes, focus;
* terminal labels ``herdr.space`` / ``herdr.tab`` to adopt a core terminal
  into the window, plus our own ``herdr.plugin`` label to find it again;
* mods (JavaScript) for UI, keys and startup — see ``app/mods``.

Plugin logic (collectors, retry state machines, renderers) is unchanged from
the TUI instance; only this integration surface differs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_LABEL = "herdr.plugin"
SPACE_LABEL = "herdr.space"
TAB_LABEL = "herdr.tab"
DEFAULT_SPACE_NAME = "dashboards"

# Windows: never allocate a console window for child processes. The guard and
# the mod's exec helpers run without a console; without this flag every hc
# invocation would pop a visible console window on the person's desktop.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class AppError(RuntimeError):
    """A hc / herdr-app command failed."""


# --------------------------------------------------------------------------
# Paths


def app_home() -> Path:
    """The window's home (settings.json, mods/, layouts/)."""
    value = os.environ.get("HERDR_APP_HOME")
    if value:
        return Path(value)
    return Path.home() / ".config" / "herdr-app"


def install_root() -> Path:
    """Where install.py placed this plugin tree (lib/ + plugins/).

    Defaults to the checkout this file lives in, so a linked development
    install works without any bookkeeping.
    """
    value = os.environ.get("HERDR_APP_PLUGINS_ROOT")
    if value:
        return Path(value)
    return Path(__file__).resolve().parent.parent


def config_dir(plugin_id: str) -> Path:
    """Operator configuration, outside Git (TUI: herdr plugin config-dir)."""
    value = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    path = Path(value) if value else app_home() / "plugins" / "config" / plugin_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir(plugin_id: str) -> Path:
    """Runtime state (TUI: HERDR_PLUGIN_STATE_DIR)."""
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    path = Path(value) if value else Path.home() / ".local" / "state" / "herdr-app" / plugin_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def python_executable() -> str:
    """Absolute interpreter for pane commands (TUI used /usr/bin/python3)."""
    if sys.executable:
        return sys.executable
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise AppError("no Python interpreter found")


def hc_binary() -> str:
    found = shutil.which("hc")
    if not found:
        raise AppError("hc (herdr-core) is not on PATH")
    return found


def app_binary() -> str:
    found = shutil.which("herdr-app")
    if not found:
        raise AppError("herdr-app is not on PATH")
    return found


# --------------------------------------------------------------------------
# Command plumbing


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=NO_WINDOW,
    )


def _parse_json(result: subprocess.CompletedProcess, command: list[str]) -> Any:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        try:
            error = json.loads(detail).get("error", {})
            detail = f"{error.get('kind', 'error')}: {error.get('message', '')}"
        except (json.JSONDecodeError, AttributeError):
            pass
        raise AppError(f"{' '.join(command[:3])} failed: {detail[:400]}")
    text = result.stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"{' '.join(command[:3])} returned invalid JSON") from exc


def hc(*args: str, timeout: float = 20) -> Any:
    """Run hc with JSON output (``hc ls --json`` etc.)."""
    command = [hc_binary(), *args]
    if "--json" not in args:
        command.append("--json")
    return _parse_json(_run(command, timeout), command)


def hc_text(*args: str, timeout: float = 20) -> str:
    """Run hc for its text output (``hc read`` prints text)."""
    command = [hc_binary(), *args]
    result = _run(command, timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AppError(f"hc {args[0] if args else ''} failed: {detail[:400]}")
    return result.stdout


def app_do(command: str, args: dict[str, Any] | None = None, timeout: float = 20) -> dict:
    """``herdr-app do <command> '<json>'`` — what the person sees."""
    argv = [app_binary(), "do", command]
    if args:
        argv.append(json.dumps(args, ensure_ascii=False))
    result = _run(argv, timeout)
    parsed = _parse_json(result, argv)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise AppError(f"{command}: {parsed['error']}")
    return parsed if isinstance(parsed, dict) else {}


def app_ls(timeout: float = 15) -> dict:
    return _parse_json(_run([app_binary(), "ls"], timeout), ["herdr-app", "ls"])


def app_status(timeout: float = 15) -> dict:
    return _parse_json(_run([app_binary(), "status"], timeout), ["herdr-app", "status"])


# --------------------------------------------------------------------------
# Terminals


def terminals(all_states: bool = False) -> list[dict]:
    args = ["ls"]
    if all_states:
        args.append("--all")
    return hc(*args).get("terminals", [])


def live_terminals() -> list[dict]:
    return [t for t in terminals() if t.get("state") == "live"]


def terminal_status(terminal: dict) -> str:
    status = terminal.get("status")
    if isinstance(status, dict):
        return str(status.get("state") or "")
    return str(status or "")


def terminal_app(terminal: dict) -> str:
    status = terminal.get("status")
    if isinstance(status, dict):
        return str(status.get("app") or "").casefold()
    return ""


def find_by_label(key: str, value: str, **extra: str) -> dict | None:
    """Find a live terminal by labels (server-side filter).

    ``hc ls --json`` returns compact records without labels, so matching is
    done by the core via ``--label`` (repeatable), not client-side.
    """
    args = ["ls", "--label", f"{key}={value}"]
    for extra_key, extra_value in extra.items():
        args += ["--label", f"{extra_key}={extra_value}"]
    try:
        found = hc(*args).get("terminals", [])
    except AppError:
        return None
    for terminal in found:
        if terminal.get("state") == "live":
            return terminal
    return None


def plugin_terminal(plugin_id: str) -> dict | None:
    return find_by_label(PLUGIN_LABEL, plugin_id)


def terminal_record(address: str) -> dict:
    """Full terminal record (argv, labels, foreground) via ``hc get``."""
    try:
        return hc("get", address, timeout=10)
    except AppError:
        return {}


def split_address(address: str) -> tuple[str | None, str]:
    """``dev/t3`` -> (``dev``, ``t3``); ``t3`` -> (None, ``t3``)."""
    if "/" in address:
        machine, local = address.split("/", 1)
        return machine, local
    return None, address


def local_id(identifier: str) -> str:
    """Strip a machine prefix: labels on a machine use its local ids."""
    return identifier.split("/", 1)[1] if "/" in identifier else identifier


# --------------------------------------------------------------------------
# Spaces / panes


def find_space(name: str, machine: str | None = None) -> dict | None:
    for space in app_ls().get("spaces", []):
        if space.get("name") == name and space.get("machine") == machine:
            return space
    return None


def ensure_space(name: str = DEFAULT_SPACE_NAME, machine: str | None = None,
                 folder: str | None = None) -> dict:
    """Find the named space or create it (first tab included)."""
    space = find_space(name, machine)
    if space is not None:
        return space
    args: dict[str, Any] = {"name": name}
    if machine:
        args["machine"] = machine
    if folder:
        args["folder"] = folder
    app_do("space.new", args)
    space = find_space(name, machine)
    if space is None:
        raise AppError(f"space {name!r} was not created")
    return space


def terminal_location(address: str) -> dict | None:
    """Locate a terminal in the window: {space, tab, pane} ids (prefixed)."""
    for space in app_ls().get("spaces", []):
        for tab in space.get("tabs", []):
            for terminal in tab.get("terminals", []):
                if terminal.get("terminal") == address:
                    return {
                        "space": space["id"],
                        "tab": tab["id"],
                        "pane": terminal.get("pane"),
                    }
    return None


def focus_terminal(address: str) -> bool:
    """Select the space/tab and focus the pane holding this terminal."""
    location = terminal_location(address)
    if location is None:
        return False
    app_do("space.select", {"id": location["space"]})
    app_do("tab.select", {"id": location["tab"]})
    if location.get("pane"):
        app_do("pane.focus", {"pane": location["pane"]})
    return True


def run_plugin_terminal(
    plugin_id: str,
    argv: list[str],
    name: str,
    cwd: str | None = None,
    machine: str | None = None,
    space_name: str = DEFAULT_SPACE_NAME,
    extra_labels: dict[str, str] | None = None,
) -> str:
    """Start a kept core terminal adopted into the plugin space.

    The TUI equivalent was ``herdr plugin pane open``. Adoption happens via
    the ``herdr.space``/``herdr.tab`` labels; ``herdr.plugin`` lets the open
    action find the same terminal again. Creating it does not select the
    space, so background restoration never steals focus.
    """
    space = ensure_space(space_name, machine)
    labels = {
        SPACE_LABEL: local_id(space["id"]),
        PLUGIN_LABEL: plugin_id,
    }
    tabs = space.get("tabs") or []
    if tabs:
        labels[TAB_LABEL] = local_id(tabs[0]["id"])
    if extra_labels:
        labels.update(extra_labels)
    command = [
        hc_binary(), "run", "--bg", "--keep", "--no-parent", "--outlive-parent",
        "--name", name,
    ]
    if machine:
        command += ["--on", machine]
    if cwd:
        command += ["--cwd", cwd]
    for key, value in labels.items():
        command += ["--label", f"{key}={value}"]
    command += ["--", *argv]
    result = _run(command, 30)
    parsed = _parse_json(result, command)
    address = parsed.get("address") or parsed.get("terminal")
    if not address:
        raise AppError(f"hc run did not return a terminal address: {parsed}")
    return str(address)


def open_plugin_pane(
    plugin_id: str,
    argv: list[str],
    name: str,
    cwd: str | None = None,
    machine: str | None = None,
    space_name: str = DEFAULT_SPACE_NAME,
    focus: bool = True,
) -> str:
    """Open or focus the plugin's terminal (TUI: open-or-focus action)."""
    existing = plugin_terminal(plugin_id)
    if existing is not None:
        address = str(existing["address"])
        if focus:
            focus_terminal(address)
        return address
    address = run_plugin_terminal(plugin_id, argv, name, cwd, machine, space_name)
    if focus:
        # Adoption is asynchronous; give the window a moment to place it.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if focus_terminal(address):
                break
            time.sleep(0.25)
    return address


# --------------------------------------------------------------------------
# Cross-platform process utilities (TUI used fcntl / /proc / SIGTERM)


class FileLock:
    """Non-blocking single-instance lock, POSIX flock / Windows msvcrt."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None
        self._locked = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            return True
        except (BlockingIOError, OSError):
            self._file.close()
            self._file = None
            return False

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._file.close()
            self._file = None
            self._locked = False

    def __enter__(self):
        if not self.acquire():
            raise BlockingIOError(f"lock busy: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def request_stop(directory: Path) -> None:
    """Ask a running monitor to exit (cross-platform; no signals needed)."""
    (directory / "stop").write_text(str(time.time()), encoding="ascii")


def stop_requested(directory: Path) -> bool:
    return (directory / "stop").exists()


def clear_stop(directory: Path) -> None:
    try:
        (directory / "stop").unlink()
    except OSError:
        pass
