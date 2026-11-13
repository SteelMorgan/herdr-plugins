#!/usr/bin/env python3
"""Install the Herdr App plugin instance — locally and on managed machines.

Local install (the window's machine):
    python install.py [--link] [--no-keys] [--no-startup]
                      [--default-machine dev] [--space dashboards]

Managed machine (scripts only; the window and mod stay local):
    python install.py --machine dev [--machine 1c]

What it does:
* copies app/lib + app/plugins to <appHome>/herdr-plugins/ (or points the
  registry at this checkout with --link) and app/mods/herdr-plugins to
  <appHome>/mods/herdr-plugins;
* writes <appHome>/herdr-plugins.json — the registry the mod reads
  (root, python, default machine, per-machine roots);
* merges app/keys/keys.fragment.json into keys.json (only free keys);
* optionally runs the startup hooks once (dashboard restorer + guard start);
* for --machine: uploads lib + plugins over `hc exec -i` (tar), detects the
  remote interpreter, and records the machine in the local registry.

Secrets, configs and state are never touched: operator configuration lives in
<appHome>/plugins/config/<id> (created on first use), state under
~/.local/state/herdr-app/.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

APP_SRC = Path(__file__).resolve().parent
REPO_APP = APP_SRC
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def app_home() -> Path:
    value = os.environ.get("HERDR_APP_HOME")
    return Path(value) if value else Path.home() / ".config" / "herdr-app"


def hc(*args: str, input_bytes: bytes | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["hc", *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=NO_WINDOW,
    )


def hc_json(*args: str, timeout: float = 30) -> dict:
    result = hc(*args, "--json", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).decode(errors="replace").strip()[:300])
    return json.loads(result.stdout.decode(errors="replace") or "{}")


def copy_tree(src: Path, dst: Path) -> None:
    """Copy a tree, skipping caches and secrets; never delete extra files."""
    for path in src.rglob("*"):
        relative = path.relative_to(src)
        parts = set(relative.parts)
        if parts & {"__pycache__", ".secrets", ".git"}:
            continue
        target = dst / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def make_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("lib", "plugins"):
            source = REPO_APP / name
            for path in sorted(source.rglob("*")):
                relative = path.relative_to(REPO_APP)
                if set(relative.parts) & {"__pycache__", ".secrets", ".git"}:
                    continue
                archive.add(path, arcname=str(relative).replace(os.sep, "/"), recursive=False)
    return buffer.getvalue()


def merge_keys(home: Path) -> list[str]:
    fragment = json.loads((REPO_APP / "keys" / "keys.fragment.json").read_text(encoding="utf-8"))
    keys_path = home / "keys.json"
    try:
        keys = json.loads(keys_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        keys = {}
    added, conflicts = [], []
    for section, bindings in fragment.items():
        section_keys = keys.setdefault(section, {})
        for chord, action in bindings.items():
            if chord in section_keys:
                if section_keys[chord] != action:
                    conflicts.append(f"{section} {chord} (kept {section_keys[chord]})")
                continue
            section_keys[chord] = action
            added.append(f"{section} {chord} -> {action}")
    if added:
        keys_path.write_text(json.dumps(keys, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return added + [f"CONFLICT: {c}" for c in conflicts]


def local_python(override: str | None) -> str:
    if override:
        return override
    return sys.executable or shutil.which("python3") or shutil.which("python") or "python3"


def install_local(args: argparse.Namespace) -> dict:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    root = REPO_APP if args.link else home / "herdr-plugins"
    if not args.link:
        copy_tree(REPO_APP / "lib", root / "lib")
        copy_tree(REPO_APP / "plugins", root / "plugins")
    copy_tree(REPO_APP / "mods" / "herdr-plugins", home / "mods" / "herdr-plugins")

    registry_path = home / "herdr-plugins.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        registry = {}
    registry.update(
        {
            "root": str(root).replace(os.sep, "/"),
            "python": local_python(args.python),
            "space": args.space,
            "machine": args.default_machine,
        }
    )
    registry.setdefault("machines", {})
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"registry: {registry_path}")
    print(f"root:     {root}")
    print(f"mod:      {home / 'mods' / 'herdr-plugins'}")

    if not args.no_keys:
        for line in merge_keys(home):
            print(f"keys: {line}")
        print("note: an edited keys.json no longer receives new default shortcuts automatically.")

    if not args.no_startup:
        restorer = root / "plugins" / "herdr-resources" / "bin" / "restore-dashboards.py"
        guard = root / "plugins" / "herdr-capacity-guard" / "bin" / "guard.py"
        command = [registry["python"], str(restorer), "--space", args.space]
        if args.default_machine:
            command += ["--machine", args.default_machine]
        print("startup: restoring dashboards (bounded 60s, no focus changes)…")
        subprocess.run(command, timeout=120, check=False, creationflags=NO_WINDOW)
        print("startup: starting agent guard…")
        subprocess.run([registry["python"], str(guard), "start"], timeout=30, check=False, creationflags=NO_WINDOW)

    check = shutil.which("herdr-app")
    if check:
        result = subprocess.run(
            [check, "check", str(home / "mods" / "herdr-plugins")],
            capture_output=True, text=True, timeout=60, check=False, creationflags=NO_WINDOW,
        )
        print(f"herdr-app check: {'PASS' if result.returncode == 0 else 'FAIL'}")
        if result.returncode != 0:
            print((result.stdout + result.stderr).strip()[:800])
    return registry


def install_machine(args: argparse.Namespace, machine: str, registry: dict) -> None:
    machines = hc_json("machine", "ls").get("machines", [])
    info = next((m for m in machines if m.get("name") == machine), None)
    if info is None:
        raise RuntimeError(f"machine {machine!r} is not configured (hc machine ls)")
    if info.get("state") != "connected":
        raise RuntimeError(f"machine {machine!r} is {info.get('state')}; connect it first")

    home_result = hc("exec", "--on", machine, "sh", "-c", 'printf %s "$HOME"', timeout=30)
    remote_home = home_result.stdout.decode(errors="replace").strip()
    if home_result.returncode != 0 or not remote_home:
        raise RuntimeError(f"could not read $HOME on {machine}")
    remote_root = f"{remote_home}/.config/herdr-app/herdr-plugins"

    python_result = hc(
        "exec", "--on", machine, "sh", "-c",
        "command -v python3 || command -v python", timeout=30,
    )
    remote_python = python_result.stdout.decode(errors="replace").strip().splitlines()[0] if python_result.returncode == 0 else ""
    if not remote_python:
        raise RuntimeError(f"no python3/python on {machine}")

    print(f"machine {machine}: uploading lib + plugins to {remote_root} …")
    archive = make_archive()
    extract = hc(
        "exec", "-i", "--on", machine, "sh", "-c",
        f"mkdir -p '{remote_root}' && tar xzf - -C '{remote_root}'",
        input_bytes=archive, timeout=120,
    )
    if extract.returncode != 0:
        raise RuntimeError(
            f"upload to {machine} failed: "
            + (extract.stderr or extract.stdout).decode(errors="replace").strip()[:300]
        )
    version = hc("exec", "--on", machine, remote_python, "--version", timeout=30)
    print(f"machine {machine}: {remote_python} ({version.stdout.decode(errors='replace').strip()})")

    registry.setdefault("machines", {})[machine] = {"root": remote_root, "python": remote_python}
    registry_path = app_home() / "herdr-plugins.json"
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"machine {machine}: recorded in {registry_path}")
    print(f"machine {machine}: scripts installed. The mod/window stay local; the central guard watches this machine.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--machine", action="append", default=[], help="managed machine to install scripts on (repeatable)")
    parser.add_argument("--link", action="store_true", help="development install: registry points at this checkout")
    parser.add_argument("--python", help="override the local interpreter")
    parser.add_argument("--space", default="dashboards", help="dashboards space name")
    parser.add_argument("--default-machine", help="machine where dashboards run (default: this computer)")
    parser.add_argument("--no-keys", action="store_true", help="skip keys.json merge")
    parser.add_argument("--no-startup", action="store_true", help="skip restorer/guard startup run")
    args = parser.parse_args(argv[1:])

    try:
        registry = install_local(args)
        for machine in args.machine:
            install_machine(args, machine, registry)
    except Exception as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
