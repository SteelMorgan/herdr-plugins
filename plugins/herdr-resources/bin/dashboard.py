#!/usr/bin/python3
"""Responsive Herdr dashboard for mirror-connected dev-containers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tomllib
import tty
from dataclasses import dataclass
from pathlib import Path

ESC = "\x1b["
RESET = f"{ESC}0m"
BG = f"{ESC}48;2;30;30;46m"
PANEL = f"{ESC}48;2;36;36;54m"
TEXT = f"{ESC}38;2;202;211;245m"
MUTED = f"{ESC}38;2;127;132;156m"
GREEN = f"{ESC}38;2;166;227;161m"
YELLOW = f"{ESC}38;2;249;226;175m"
RED = f"{ESC}38;2;243;139;168m"
MAUVE = f"{ESC}38;2;203;166;247m"
BLUE = f"{ESC}38;2;137;180;250m"
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Docker Desktop's Linux CLI autolaunches a D-Bus session bus on every
# `docker info`/`docker stats` when none is set, and those daemons never exit.
# Polling leaked ~900/hour and exhausted WSL memory, so disable autolaunch
# unless the environment already provides a shared bus (~/.config/session-bus.sh).
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "disabled:")

# SSH hosts are sampled less often than the TUI refreshes. One short-lived
# command reads procfs and the root filesystem; no remote agent is installed.
SSH_PROBE_INTERVAL_SECONDS = 15.0
SSH_PROBE_TIMEOUT_SECONDS = 8.0
SSH_PROBE_SCRIPT = r'''set -eu
cpu_sample() {
    awk '$1 == "cpu" { idle = $5 + $6; total = 0; for (i = 2; i <= 8; i++) total += $i; print total, idle; exit }' /proc/stat
}
before=$(cpu_sample)
sleep 0.2
after=$(cpu_sample)
awk -v before="$before" -v after="$after" 'BEGIN {
    split(before, b); split(after, a)
    total = a[1] - b[1]; idle = a[2] - b[2]
    if (total > 0) printf "cpu %.1f\n", (total - idle) * 100 / total
    else print "cpu"
}'
awk '
    /^MemTotal:/ { total = $2 }
    /^MemAvailable:/ { available = $2 }
    END {
        if (total > 0) printf "ram %.1f %d %d\n", (total - available) * 100 / total, total - available, total
        else print "ram"
    }
' /proc/meminfo
df -Pk / | awk 'NR == 2 { print "disk " $2 " " $3 " " $5 }'
'''
SSH_PROBE_LOCK = threading.Lock()
SSH_PROBE_CACHE: dict[str, tuple[float, subprocess.CompletedProcess[str]]] = {}


@dataclass
class Host:
    key: str
    prefix: str
    kind: str = "ssh"
    enabled: bool = True
    container: str = ""
    state: str = "unknown"
    running: bool = False
    uptime: str = ""
    cpu: float | None = None
    memory: float | None = None
    memory_used: str = ""
    memory_limit: str = ""
    disk: float | None = None
    disk_used: str = ""
    disk_total: str = ""
    herdr: str = "down"
    oom_recent: bool = False
    error: str = ""


@dataclass
class DockerEngine:
    running: int = 0
    cpu: float | None = None
    cpu_detail: str = ""
    memory: float | None = None
    memory_detail: str = ""
    disk: float | None = None
    disk_detail: str = ""


ENGINE = DockerEngine()


def run(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return subprocess.CompletedProcess(args, 124, "", f"timeout after {timeout:g}s: {stderr}")
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def parse_number(value: object, default: float | None = None) -> float | None:
    """Parse Docker's human-facing numeric fields without crashing the TUI."""
    try:
        parsed = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def configured_hosts() -> list[dict[str, object]]:
    path = Path.home() / ".config/herdr-mirror/hosts.toml"
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    return [{"key": key, **value} for key, value in data.get("hosts", {}).items()]


def resolve_container(config: dict[str, object]) -> str:
    if config.get("container"):
        return str(config["container"])
    folder = str(config.get("folder") or "")
    if not folder:
        return ""
    result = run(["docker", "ps", "-aq", "--filter", f"label=devcontainer.local_folder={folder}"])
    ids = result.stdout.split()
    if not ids:
        return ""
    found = run(["docker", "inspect", "--format", "{{.Name}}", ids[0]])
    return found.stdout.strip().lstrip("/")


def parse_size(value: str) -> str:
    match = re.match(r"([0-9.]+)\s*([KMGT]?i?B)", value.strip(), re.I)
    if not match:
        return value.strip()
    number, unit = float(match.group(1)), match.group(2)
    return f"{number:.1f}{unit}" if number < 10 else f"{number:.0f}{unit}"


def human_kib(value: int) -> str:
    gib = value / 1024 / 1024
    return f"{gib:.1f}G" if gib < 10 else f"{gib:.0f}G"


def ssh_probe(target: str) -> subprocess.CompletedProcess[str]:
    """Collect one cheap remote sample, reusing it between TUI refreshes."""
    now = time.monotonic()
    with SSH_PROBE_LOCK:
        cached = SSH_PROBE_CACHE.get(target)
        if cached and now - cached[0] < SSH_PROBE_INTERVAL_SECONDS:
            return cached[1]
    command = ["ssh"]
    user_config = Path.home() / ".ssh/config"
    if user_config.is_file():
        command.extend(["-F", str(user_config)])
    command.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=3", target, SSH_PROBE_SCRIPT])
    result = run(command, timeout=SSH_PROBE_TIMEOUT_SECONDS)
    with SSH_PROBE_LOCK:
        SSH_PROBE_CACHE[target] = (time.monotonic(), result)
    return result


def apply_ssh_metrics(host: Host, output: str) -> None:
    """Parse the fixed probe output without relying on remote utilities."""
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu" and len(fields) >= 2:
            value = parse_number(fields[1])
            host.cpu = None if value is None else max(0.0, min(100.0, value))
        elif fields[0] == "ram" and len(fields) >= 4:
            value = parse_number(fields[1])
            host.memory = None if value is None else max(0.0, min(100.0, value))
            try:
                host.memory_used = human_kib(int(fields[2]))
                host.memory_limit = human_kib(int(fields[3]))
            except ValueError:
                pass
        elif fields[0] == "disk" and len(fields) >= 4:
            host.disk = parse_number(fields[3])
            try:
                host.disk_total = human_kib(int(fields[1]))
                host.disk_used = human_kib(int(fields[2]))
            except ValueError:
                pass


def mirror_state() -> tuple[bool, dict[str, bool]]:
    candidates = sorted((Path.home() / ".config/herdr/plugins/github").glob("mirror-*/target/release/herdr-mirror"))
    if not candidates:
        return False, {}
    result = run([str(candidates[-1]), "status"], timeout=8)
    text = result.stdout + result.stderr
    daemon = result.returncode == 0 and "daemon: running" in text
    linked: dict[str, bool] = {}
    for key, workspaces, panes in re.findall(r"host\s+(\S+)\s+\([^)]*\):\s+(\d+)\s+mirror workspaces?,\s+(\d+)\s+mirror panes?", text):
        linked[key] = int(workspaces) > 0 or int(panes) > 0
    return daemon, linked


def collect_one(config: dict[str, object], linked: dict[str, bool]) -> Host:
    key = str(config["key"])
    kind = str(config.get("kind") or "ssh")
    host = Host(key=key, prefix=str(config.get("prefix") or key).upper(), kind=kind)
    if config.get("enabled") is False:
        host.enabled = False
        host.state = "disabled"
        host.herdr = "disabled"
        host.container = str(config.get("container") or config.get("target") or "")
        return host
    try:
        if kind != "docker":
            target = str(config.get("target") or "")
            probe = ssh_probe(target) if target else None
            host.container = target
            host.running = bool(probe and probe.returncode == 0)
            host.state = "running" if host.running else "offline"
            host.herdr = "linked" if host.running and linked.get(key) else "down"
            if not host.running:
                host.error = "SSH host unreachable"
            else:
                apply_ssh_metrics(host, probe.stdout)
            return host
        host.container = resolve_container(config)
        if not host.container:
            host.state, host.error = "missing", "container not found"
            return host
        inspected = run(["docker", "inspect", "--format", "{{json .State}}", host.container])
        if inspected.returncode:
            host.state, host.error = "missing", inspected.stderr.strip()
            return host
        state = json.loads(inspected.stdout)
        host.state = str(state.get("Status") or "unknown")
        host.running = bool(state.get("Running"))
        if not host.running:
            return host
        ps = run(["docker", "ps", "--filter", f"name=^{host.container}$", "--format", "{{.Status}}"])
        host.uptime = ps.stdout.strip().removeprefix("Up ").split(" (")[0]
        stats = run(["docker", "stats", "--no-stream", "--format", "{{json .}}", host.container], timeout=8)
        if stats.returncode == 0 and stats.stdout.strip():
            data = json.loads(stats.stdout.splitlines()[0])
            host.cpu = parse_number(data.get("CPUPerc"))
            host.memory = parse_number(data.get("MemPerc"))
            usage = str(data.get("MemUsage") or "").split("/")
            if len(usage) == 2:
                host.memory_used, host.memory_limit = map(parse_size, usage)
        disk = run(["docker", "exec", host.container, "df", "-Pk", "/"], timeout=5)
        lines = disk.stdout.splitlines()
        if disk.returncode == 0 and len(lines) >= 2:
            fields = lines[-1].split()
            host.disk_total, host.disk_used = human_kib(int(fields[1])), human_kib(int(fields[2]))
            host.disk = parse_number(fields[4])
        remote = str(config.get("remote_bin") or "herdr")
        command = ["docker", "exec", host.container, *remote.split(), "session", "list", "--json"]
        session = run(command, timeout=5)
        if session.returncode == 0:
            sessions = json.loads(session.stdout).get("sessions", [])
            running = any(item.get("running") for item in sessions)
            host.herdr = "linked" if running and linked.get(key) else "ready" if running else "down"
        else:
            host.herdr = "down"
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        host.error = str(exc)
    return host


def collect() -> tuple[bool, list[Host]]:
    daemon, linked = mirror_state()
    configs = configured_hosts()
    hosts: list[Host] = []
    lock = threading.Lock()

    def worker(config: dict[str, object]) -> None:
        try:
            item = collect_one(config, linked)
        except Exception as exc:  # A single malformed host must not kill refresh.
            item = Host(
                key=str(config.get("key") or "unknown"),
                prefix=str(config.get("prefix") or config.get("key") or "unknown").upper(),
                kind=str(config.get("kind") or "ssh"),
                error=f"collector failed: {exc}",
            )
        with lock:
            hosts.append(item)

    threads = [threading.Thread(target=worker, args=(config,)) for config in configs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    oom_containers = recent_oom_containers()
    for host in hosts:
        host.oom_recent = host.kind == "docker" and host.container in oom_containers
    order = {str(config["key"]): index for index, config in enumerate(configs)}
    hosts.sort(key=lambda item: order[item.key])
    global ENGINE
    ENGINE = collect_docker_engine(hosts)
    return daemon, hosts


def color_for(value: float | None) -> str:
    if value is None:
        return MUTED
    if value >= 90:
        return RED
    if value >= 70:
        return YELLOW
    return GREEN


def recent_oom_containers() -> set[str]:
    result = run([
        "docker", "events", "--since", "15m", "--until", "0s",
        "--filter", "type=container", "--filter", "event=oom", "--format", "{{json .}}",
    ], timeout=5)
    containers: set[str] = set()
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        actor = event.get("Actor") or {}
        attributes = actor.get("Attributes") or {}
        for value in (attributes.get("name"), event.get("id"), actor.get("ID")):
            if value:
                containers.add(str(value).lstrip("/"))
    return containers


def fit(text: str, width: int) -> str:
    return text[:width].ljust(width)


def fit_ansi(text: str, width: int) -> str:
    visible = len(ANSI_RE.sub("", text))
    return text + " " * max(0, width - visible)


def bar(value: float | None, width: int) -> str:
    if width <= 0:
        return ""
    used = 0 if value is None else max(0, min(width, round(value * width / 100)))
    return f"{color_for(value)}{'━' * used}{MUTED}{'─' * (width - used)}{TEXT}"


def metric(label: str, value: float | None, detail: str, inner: int, compact: bool) -> str:
    shown = "--" if value is None else f"{value:.0f}%" if compact else f"{value:5.1f}%"
    if compact:
        bw = max(1, inner - 10)
        plain = f"{label:<4} {shown:>4} "
        return plain + bar(value, bw)
    suffix = f"  {detail}" if detail else ""
    bw = max(5, inner - 32)
    return f"{label:<4} {bar(value, bw)} {color_for(value)}{shown}{TEXT}{MUTED}{suffix}{TEXT}"


def size_bytes(value: str) -> float:
    match = re.match(r"([0-9.]+)\s*([KMGT]?i?B)", value, re.I)
    if not match:
        return 0
    units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
             "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
    return float(match.group(1)) * units.get(match.group(2).upper(), 1)


def human_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
        value /= 1024
    return "--"


def memory_pair(host: Host, compact: bool = False) -> str:
    used, limit = host.memory_used, host.memory_limit
    used_match = re.match(r"([0-9.]+)([A-Za-z]+)", used)
    limit_match = re.match(r"([0-9.]+)([A-Za-z]+)", limit)
    if used_match and limit_match and used_match.group(2).lower() == limit_match.group(2).lower():
        pair = f"{used_match.group(1)}/{limit_match.group(1)}{limit_match.group(2)}"
    else:
        pair = f"{used}/{limit}".strip("/")
    return pair.replace("GiB", "G").replace("MiB", "M") if compact else pair


def collect_docker_engine(hosts: list[Host]) -> DockerEngine:
    active = [host for host in hosts if host.kind == "docker" and host.running]
    if not active:
        return DockerEngine()
    engine = DockerEngine()
    info_result = run(["docker", "info", "--format", "{{json .}}"], timeout=5)
    try:
        parsed_info = json.loads(info_result.stdout)
        info = parsed_info if isinstance(parsed_info, dict) else {}
    except (ValueError, json.JSONDecodeError):
        info = {}
    stats = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=12)
    rows = []
    for line in stats.stdout.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            continue
    raw_cpu = sum(parse_number(row.get("CPUPerc"), 0) or 0 for row in rows)
    cpus = max(1, int(parse_number(info.get("NCPU"), 1) or 1))
    engine.cpu = raw_cpu / cpus
    engine.cpu_detail = f"{raw_cpu:.0f}%/{cpus * 100}%"
    memory_used = sum(size_bytes(str(row.get("MemUsage") or "").split("/")[0]) for row in rows)
    memory_limit = parse_number(info.get("MemTotal"), 0) or 0
    engine.memory = memory_used / memory_limit * 100 if memory_limit else None
    engine.memory_detail = f"{human_bytes(memory_used)}/{human_bytes(memory_limit)}" if memory_limit else ""
    engine.running = max(0, int(parse_number(info.get("ContainersRunning"), len(rows)) or 0))
    first_disk = next((host for host in active if host.disk is not None), active[0])
    engine.disk = first_disk.disk
    engine.disk_detail = f"{first_disk.disk_used}/{first_disk.disk_total}".strip("/")
    return engine


def render(daemon: bool, hosts: list[Host], width: int, now: float) -> str:
    width = max(20, width)
    inner = width - 4
    compact = width < 42
    daemon_text = "● LIVE" if daemon else "● DOWN"
    daemon_color = GREEN if daemon else RED
    title = "Mirror fleet"
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    heading = f"  Mirror  {daemon_text}" if compact else f"  {title}  {daemon_text}  {stamp}"
    lines = [f"{BG}{TEXT}{fit('', width)}", f"{BG}{MAUVE}{fit_ansi(heading, width)}{RESET}"]
    lines.append(f"{BG}{MUTED}{fit('  configured dev-containers', width)}")
    docker_hosts = [host for host in hosts if host.kind == "docker"]
    ssh_hosts = [host for host in hosts if host.kind != "docker"]
    if docker_hosts:
        lines.append(f"{BG}{TEXT}{fit('', width)}")
        docker_header = (f"LOCAL DOCKER · {ENGINE.running} RUNNING · {sum(h.enabled for h in docker_hosts)} MIRRORED"
                         if not compact else f"DOCKER {ENGINE.running}↑ · {sum(h.enabled for h in docker_hosts)}M")
        lines.append(f"{BG}{PANEL}{MAUVE}  {fit(docker_header, inner)}  {RESET}")
        for label, value, detail in (
            ("CPU", ENGINE.cpu, ENGINE.cpu_detail),
            ("RAM", ENGINE.memory, ENGINE.memory_detail),
            ("DISK", ENGINE.disk, ENGINE.disk_detail),
        ):
            content = metric(label, value, detail, inner, compact)
            lines.append(f"{BG}{PANEL}{TEXT}  {fit_ansi(content, inner)}  {RESET}")
        lines.append(f"{BG}{PANEL}{TEXT}  {fit('', inner)}  {RESET}")
        lines.append(f"{BG}{PANEL}{MUTED}  {fit('CONTAINERS', inner)}  {RESET}")
        for index, host in enumerate(docker_hosts):
            branch = "└─" if index == len(docker_hosts) - 1 else "├─"
            continuation = "   " if index == len(docker_hosts) - 1 else "│  "
            if not host.enabled:
                child = f"{branch} {host.prefix} ○ DISABLED" if compact else f"{branch} {host.prefix:<4} ○ DISABLED · mirror excluded"
                lines.append(f"{BG}{PANEL}{MUTED}  {fit(child, inner)}  {RESET}")
                continue
            online = host.running
            state = "● ONLINE" if online else "● OFFLINE"
            herdr = {"linked": "H●LINKED", "ready": "H●READY", "down": "H●DOWN"}.get(host.herdr, "H●DOWN")
            state_color = RED if host.oom_recent else GREEN if online and host.herdr == "linked" else YELLOW if online else RED
            oom = "  ⚠ OOM≤15m" if host.oom_recent else ""
            if compact:
                container_short = "C●ON" if online else "C●OFF"
                if inner < 22 and host.oom_recent:
                    child = f"{branch} {host.prefix} ⚠OOM {container_short}"
                else:
                    herdr_short = {"linked": "H●LINK", "ready": "H●READY", "down": "H●DOWN"}.get(host.herdr, "H●DOWN")
                    oom_short = " ⚠OOM" if host.oom_recent else ""
                    child = f"{branch} {host.prefix} {container_short} {herdr_short}{oom_short}"
            else:
                child = f"{branch} {host.prefix:<4} {state}  {herdr}{oom}"
            cpu_percent = "--" if host.cpu is None else f"{host.cpu:.1f}%"
            ram_percent = "--" if host.memory is None else f"{host.memory:.0f}%"
            metrics = f"CPU {cpu_percent}  RAM {ram_percent}"
            combined = f"{child}  {metrics}"
            if len(combined) <= inner:
                lines.append(f"{BG}{PANEL}{state_color}  {fit(combined, inner)}  {RESET}")
            else:
                lines.append(f"{BG}{PANEL}{state_color}  {fit(child, inner)}  {RESET}")
                child_metrics = f"{continuation}{metrics}"
                if len(child_metrics) > inner:
                    cpu_short = "--" if host.cpu is None else f"{host.cpu:.0f}%"
                    ram_short = "--" if host.memory is None else f"{host.memory:.0f}%"
                    child_metrics = f"{continuation}C{cpu_short} R{ram_short}"
                lines.append(f"{BG}{PANEL}{TEXT}  {fit(child_metrics, inner)}  {RESET}")

    for host in ssh_hosts:
        lines.append(f"{BG}{TEXT}{fit('', width)}")
        if not host.enabled:
            lines.append(f"{BG}{PANEL}{MUTED}  {fit('SSH · ' + host.prefix + ' ○ DISABLED', inner)}  {RESET}")
            continue
        online = host.running
        state = "● ONLINE" if online else "● OFFLINE"
        herdr = "H●LINKED" if host.herdr == "linked" else "H●DOWN"
        header = f"SSH · {host.prefix}" if compact else f"SSH · {host.prefix}  {host.container}"
        lines.append(f"{BG}{PANEL}{MAUVE}  {fit(header, inner)}  {RESET}")
        status = f"  {GREEN if online else RED}{state}  {herdr}{TEXT}"
        lines.append(f"{BG}{PANEL}{fit_ansi(status, width)}{RESET}")
        for label, value, detail in (("CPU", host.cpu, ""), ("RAM", host.memory, ""), ("DISK", host.disk, "")):
            content = metric(label, value, detail, inner, compact)
            lines.append(f"{BG}{PANEL}{TEXT}  {fit_ansi(content, inner)}  {RESET}")
        if host.error:
            lines.append(f"{BG}{PANEL}{RED}  {fit(host.error, inner)}  {RESET}")
    lines.append(f"{BG}{TEXT}{fit('', width)}")
    footer = "  r refresh q close" if compact else "  r refresh · q close · auto 3s"
    lines.append(f"{BG}{MUTED}{fit(footer, width)}{RESET}")
    return f"{ESC}H" + "\n".join(lines) + f"{ESC}J"


def diagnose() -> int:
    daemon, hosts = collect_resilient((False, []))
    print(json.dumps({"mirror_daemon": daemon, "hosts": [host.__dict__ for host in hosts]}, indent=2))
    return 0


def collect_resilient(previous: tuple[bool, list[Host]]) -> tuple[bool, list[Host]]:
    """Keep the last good frame when a new Docker response is unusable."""
    try:
        return collect()
    except Exception as exc:
        print(f"collector refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return previous


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    if args.diagnose:
        return diagnose()
    old = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    print(f"{ESC}?1049h{ESC}?25l", end="", flush=True)
    try:
        daemon, hosts = collect_resilient((False, []))
        refreshed = time.time()
        while True:
            width = shutil.get_terminal_size((60, 30)).columns
            print(render(daemon, hosts, width, refreshed), end="", flush=True)
            ready, _, _ = select.select([sys.stdin], [], [], max(0.1, 3 - (time.time() - refreshed)))
            if ready:
                key = os.read(sys.stdin.fileno(), 1)
                if key in (b"q", b"Q", b"\x03"):
                    break
                if key not in (b"r", b"R"):
                    continue
            daemon, hosts = collect_resilient((daemon, hosts))
            refreshed = time.time()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        print(f"{RESET}{ESC}?25h{ESC}?1049l", end="", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
