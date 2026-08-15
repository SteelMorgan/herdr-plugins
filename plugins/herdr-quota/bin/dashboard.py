#!/usr/bin/python3
"""Dependency-free terminal dashboard for Herdr model quotas."""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

AUTO_REFRESH_SECONDS = 300
CODEX_ATTEMPT_TIMEOUT_SECONDS = 5
USAGE_RESPONSE_TIMEOUT_SECONDS = 15
USAGE_PARSED_QUIET_SECONDS = 1.0
TRANSIENT_RETRY_DELAYS_SECONDS = (0.5, 1.5)
COLLECTOR_STAGGER_SECONDS = 1.5
COLLECTOR_JOIN_TIMEOUT_SECONDS = 70


@dataclass
class Window:
    label: str
    used_percent: int
    resets_at: int | None = None
    duration_minutes: int | None = None
    reset_text: str = ""


@dataclass
class Provider:
    name: str
    status: str = "loading"
    plan: str = ""
    windows: list[Window] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    error: str = ""
    updated_at: float = 0


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items = {name: Provider(name) for name in ("CODEX", "CLAUDE", "KIMI", "DEEPSEEK API")}

    def put(self, provider: Provider) -> None:
        now = time.time()
        with self.lock:
            previous = self.items[provider.name]
            if provider.status in ("ok", "depleted"):
                provider.updated_at = now
            elif provider.status in ("error", "unsupported") and previous.updated_at and previous.status in ("ok", "depleted", "stale"):
                provider = Provider(
                    provider.name,
                    status="stale",
                    plan=previous.plan,
                    windows=list(previous.windows),
                    details=list(previous.details),
                    error=provider.error or provider.status,
                    updated_at=previous.updated_at,
                )
            else:
                provider.updated_at = now
            self.items[provider.name] = provider

    def snapshot(self) -> list[Provider]:
        with self.lock:
            return [Provider(p.name, p.status, p.plan, list(p.windows), list(p.details), p.error, p.updated_at) for p in self.items.values()]


def clean_terminal(text: str) -> str:
    text = ANSI_RE.sub("", text).replace("\r", "\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def find_binary(command: str) -> str | None:
    found = shutil.which(command)
    if found:
        return found
    candidates = (
        Path.home() / ".local" / "bin" / command,
        Path.home() / ".kimi-code" / "bin" / command,
    )
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def is_transient_error(provider: Provider) -> bool:
    if provider.status != "error":
        return False
    error = provider.error.lower()
    http_status = re.search(r"\bhttp\s+(\d{3})\b", error)
    if http_status:
        code = int(http_status.group(1))
        return code == 429 or code >= 500
    return any(marker in error for marker in (
        "ssl", "tls", "eof", "timeout", "timed out", "connection",
        "network", "error sending request", "temporarily unavailable",
        "reset by peer", "name resolution", "temporary failure",
    ))


def collect_with_retry(collector: Any) -> Provider:
    provider = collector()
    for delay in TRANSIENT_RETRY_DELAYS_SECONDS:
        if not is_transient_error(provider):
            break
        time.sleep(delay)
        provider = collector()
    return provider


def format_age(updated_at: float) -> str:
    seconds = max(0, int(time.time() - updated_at))
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return format_countdown(seconds, compact=True)


def format_countdown(seconds: int | None, compact: bool = False) -> str:
    if seconds is None:
        return ""
    seconds = max(0, seconds)
    if seconds >= 86400:
        days, hours = seconds // 86400, (seconds % 86400) // 3600
        return f"{days}d{hours:02d}h" if compact else f"{days}d {hours:02d}h"
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    return f"{hours}h{minutes:02d}m" if compact else f"{hours:02d}h {minutes:02d}m"


def format_reset(epoch: int | None) -> str:
    if not epoch:
        return ""
    return format_countdown(epoch - int(time.time()))


def parse_duration_seconds(text: str) -> int | None:
    if not text:
        return None
    units = {"d": 86400, "h": 3600, "m": 60}
    total = sum(int(value) * units[unit.lower()] for value, unit in re.findall(r"(\d+)\s*([dhm])", text, re.I))
    return total or None


def burn_rate(window: Window) -> float | None:
    if not window.duration_minutes:
        return None
    duration = window.duration_minutes * 60
    until_reset = max(0, window.resets_at - int(time.time())) if window.resets_at else parse_duration_seconds(window.reset_text)
    if until_reset is None:
        return None
    elapsed = max(0, duration - min(duration, until_reset))
    if elapsed <= 0:
        return None
    return (window.used_percent / 100) / (elapsed / duration)


def format_burn(window: Window) -> str:
    rate = burn_rate(window)
    if rate is None:
        return "--"
    if rate >= 100:
        return "x99+"
    return f"x{rate:.1f}"


def _codex_collect_once(binary: str) -> Provider:
    provider = Provider("CODEX")
    proc = subprocess.Popen(
        [binary, "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    try:
        assert proc.stdin and proc.stdout
        messages = (
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "herdr-quota", "title": "Herdr Quota", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}}},
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/rateLimits/read", "params": {}},
        )
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + CODEX_ATTEMPT_TIMEOUT_SECONDS
        payload: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message", message["error"])))
                payload = message.get("result")
                break
        if not payload:
            raise TimeoutError("rate-limit response timed out")
        snapshot = payload.get("rateLimits") or {}
        provider.plan = str(snapshot.get("planType") or "subscription")
        for key, label in (("primary", "primary"), ("secondary", "secondary")):
            window = snapshot.get(key)
            if window:
                duration = window.get("windowDurationMins")
                if duration:
                    if 240 <= duration <= 360:
                        label = "5 hour"
                    elif 6500 <= duration <= 8000:
                        label = "5 day"
                    elif duration >= 9000:
                        label = "weekly"
                    else:
                        label = f"{duration // 60} hour"
                used = max(0, min(100, int(window.get("usedPercent", 0))))
                provider.windows.append(Window(label, used, window.get("resetsAt"), duration))
        credits = snapshot.get("credits") or {}
        if credits.get("balance") not in (None, "0", "0.0", "0.00"):
            provider.details.append(f"credits {credits['balance']}")
        provider.status = "ok" if provider.windows else "empty"
    except Exception as exc:
        provider.status, provider.error = "error", str(exc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    return provider


def codex_collect() -> Provider:
    binary = find_binary("codex")
    if not binary:
        return Provider("CODEX", status="unavailable", error="codex not found")
    return collect_with_retry(lambda: _codex_collect_once(binary))


def iso_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def claude_collect() -> Provider:
    """Use Claude Code's supported interactive /usage command."""
    return usage_cli_collect("CLAUDE", "claude")


def parse_claude_reset(line: str, now: datetime | None = None) -> int | None:
    """Convert Claude's absolute `Resets ...` text to a local epoch."""
    match = re.search(
        r"(?i)\bresets?\s*"
        r"(?:(?P<month>[a-z]{3,9})\s*(?P<day>\d{1,2})\s*,\s*)?"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)"
        r"(?:\s*\((?P<timezone>[^)]+)\))?",
        line,
    )
    if not match:
        return None

    timezone_name = match.group("timezone")
    timezone = datetime.now().astimezone().tzinfo
    if timezone_name:
        try:
            timezone = ZoneInfo(timezone_name.strip())
        except ZoneInfoNotFoundError:
            pass
    if timezone is None:
        return None
    current = (now or datetime.now().astimezone()).astimezone(timezone)

    hour = int(match.group("hour")) % 12
    if match.group("ampm").lower() == "pm":
        hour += 12
    minute = int(match.group("minute") or 0)
    month_text = match.group("month")
    day_text = match.group("day")
    if month_text and day_text:
        months = {name.lower(): number for number, name in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
            start=1,
        )}
        month = months.get(month_text[:3].lower())
        if month is None:
            return None
        year = current.year
        candidate = datetime(year, month, int(day_text), hour, minute, tzinfo=timezone)
        if candidate <= current:
            candidate = candidate.replace(year=year + 1)
    else:
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
    return int(candidate.timestamp())


def parse_claude_usage(text: str) -> tuple[str, list[Window]]:
    """Parse account-wide windows from Claude Code's interactive /usage screen."""
    lines = text.splitlines()
    plan = ""
    windows: list[Window] = []

    for line in lines:
        match = re.search(r"(?i)Claude\s*(Max|Pro|Team|Enterprise)", line)
        if match:
            plan = match.group(1).lower()
            break

    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line).lower()
        if compact not in ("currentsession", "currentweek(allmodels)"):
            continue
        label = "5 hour" if compact == "currentsession" else "weekly"
        duration = 300 if label == "5 hour" else 10080
        used: int | None = None
        resets_at: int | None = None
        reset_text = ""
        for following in lines[index + 1 : index + 6]:
            percent = re.search(r"(?i)\b(\d{1,3})\s*%\s*(used|remaining|left)?", following)
            if percent and used is None:
                value = min(100, int(percent.group(1)))
                sense = (percent.group(2) or "used").lower()
                used = 100 - value if sense in ("remaining", "left") else value
            reset = re.search(r"(?i)resets?\s+in\s+(.+)", following)
            if reset:
                reset_text = reset.group(1).strip()
            absolute_reset = parse_claude_reset(following)
            if absolute_reset is not None:
                resets_at = absolute_reset
            if used is not None and (resets_at is not None or reset_text):
                break
        if used is not None:
            windows.append(Window(label, used, resets_at=resets_at, duration_minutes=duration, reset_text=reset_text))

    return plan, windows


def parse_usage_text(name: str, text: str) -> Provider:
    provider = Provider(name)
    if name == "CLAUDE":
        provider.plan, provider.windows = parse_claude_usage(text)
    else:
        relevant = []
        for line in text.splitlines():
            low = line.lower()
            if any(word in low for word in ("usage", "remaining", "reset", "limit", "plan", "quota", "%", "week", "hour")):
                if line not in relevant and len(line) < 180:
                    relevant.append(line)
        # Both supported subscription CLIs describe consumption as "% used".
        # Parse only explicit limit rows; percentages elsewhere in a startup
        # screen are commonly context meters and must not become account quota.
        for line in relevant:
            match = re.search(r"(?i)\b(weekly|week|5h)\s+limit[^\n%]*?(\d{1,3})\s*%\s*(used|remaining|left)?", line)
            if not match:
                continue
            label_raw, value_raw, sense = match.groups()
            value = min(100, int(value_raw))
            used = 100 - value if sense and sense.lower() in ("remaining", "left") else value
            label = "weekly" if label_raw.lower() in ("weekly", "week") else "5 hour"
            reset_match = re.search(r"(?i)resets?\s+in\s+([^│]+)", line)
            reset_text = reset_match.group(1).strip() if reset_match else ""
            duration = 10080 if label == "weekly" else (300 if label == "5 hour" else None)
            provider.windows.append(Window(label, used, duration_minutes=duration, reset_text=reset_text))
    provider.status = "ok" if provider.windows else "unsupported"
    if provider.status == "unsupported":
        provider.error = "/usage returned no parseable quota"
    return provider


def has_usage_screen(name: str, text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).lower()
    if name == "CLAUDE":
        return "current session" in compact or "current week (all models)" in compact
    return "plan usage" in compact or "weekly limit" in compact or "5h limit" in compact


def _usage_cli_collect_once(name: str, binary: str, startup_wait: float) -> Provider:
    provider = Provider(name)
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.setdefault("TERM", "xterm-256color")
        os.execv(binary, [binary])
    output = bytearray()
    try:
        end = time.monotonic() + startup_wait
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                output.extend(os.read(fd, 65536))
        os.write(fd, b"/usage\r")
        quiet_since = time.monotonic()
        deadline = time.monotonic() + USAGE_RESPONSE_TIMEOUT_SECONDS
        parsed = Provider(name)
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.35)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                output.extend(chunk)
                quiet_since = time.monotonic()
                text = clean_terminal(output.decode("utf-8", "replace"))
                parsed = parse_usage_text(name, text)
            elif parsed.status == "ok" and time.monotonic() - quiet_since > USAGE_PARSED_QUIET_SECONDS:
                break
        text = clean_terminal(output.decode("utf-8", "replace"))
        provider = parse_usage_text(name, text)
        if provider.status == "unsupported" and not has_usage_screen(name, text):
            provider.status = "error"
            provider.error = "/usage response timed out before quota became parseable"
    except Exception as exc:
        provider.status, provider.error = "error", str(exc)
    finally:
        try:
            os.write(fd, b"\x03\x03")
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return provider


def usage_cli_collect(name: str, command: str, startup_wait: float = 5.0) -> Provider:
    binary = find_binary(command)
    if not binary:
        return Provider(name, status="unavailable", error=f"{command} not found")
    return collect_with_retry(lambda: _usage_cli_collect_once(name, binary, startup_wait))


def load_plugin_env() -> dict[str, str]:
    values: dict[str, str] = {}
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        return values
    path = Path(config_dir) / ".env"
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return values


def _deepseek_collect_once(token: str) -> Provider:
    provider = Provider("DEEPSEEK API")
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "herdr-quota/0.1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.load(response)
        balances = payload.get("balance_infos") or []
        provider.status = "ok" if payload.get("is_available") else "depleted"
        for balance in balances:
            currency = balance.get("currency", "")
            provider.details.append(f"balance {balance.get('total_balance', '?')} {currency}")
    except urllib.error.HTTPError as exc:
        provider.status, provider.error = "error", f"HTTP {exc.code}"
    except Exception as exc:
        provider.status, provider.error = "error", str(exc)
    return provider


def deepseek_collect() -> Provider:
    token = os.environ.get("DEEPSEEK_API_KEY") or load_plugin_env().get("DEEPSEEK_API_KEY")
    if not token:
        return Provider(
            "DEEPSEEK API",
            status="unconfigured",
            error="set DEEPSEEK_API_KEY in plugin config .env",
        )
    return collect_with_retry(lambda: _deepseek_collect_once(token))


def bar(percent: int, width: int) -> str:
    width = max(1, width)
    filled = round(width * max(0, min(100, percent)) / 100)
    color = RED if percent >= 80 else (YELLOW if percent >= 50 else GREEN)
    return color + "█" * filled + MUTED + "░" * (width - filled) + RESET + BG


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def render(store: Store, refreshing: bool) -> tuple[int, int, int] | None:
    width, height = shutil.get_terminal_size((62, 40))
    width = max(24, width)
    lines: list[str] = []
    config_hitbox: tuple[int, int, int] | None = None
    inner = width - 4
    lines.append(MUTED + "┌" + "─" * (width - 2) + "┐")
    stamp = datetime.now().strftime("%H:%M:%S")
    if inner >= 45:
        head = "spent · weekly pace"
        right = "updating…" if refreshing else f"updated {stamp}"
    else:
        head = "spent · weekly"
        right = ""
    gap = max(1, inner - len(head) - len(right))
    lines.append("│ " + MAUVE + head + MUTED + " " * gap + right + " │")
    lines.append("├" + "─" * (width - 2) + "┤")
    for provider in store.snapshot():
        state_color = GREEN if provider.status == "ok" else (YELLOW if provider.status in ("loading", "empty", "unsupported", "unconfigured", "stale") else RED)
        header = provider.name
        state = f"stale {format_age(provider.updated_at)}" if provider.status == "stale" else provider.status
        button = "[⚙]" if provider.name == "DEEPSEEK API" and provider.status == "unconfigured" else ""
        right = state + (f"  {button}" if button else "")
        weekly_window = next((window for window in provider.windows if window.label == "weekly"), None)
        if weekly_window:
            pace = format_burn(weekly_window)
            pace_rate = burn_rate(weekly_window)
            pace_color = RED if pace_rate is not None and pace_rate >= 1.5 else (YELLOW if pace_rate is not None and pace_rate > 1 else GREEN)
            pace_padding = max(1, 9 - len(header))
            left_width = len(header) + pace_padding + 5
            gap = max(1, inner - left_width - len(right))
            lines.append("│ " + BLUE + header + " " * pace_padding + pace_color + f"{pace:<5}" + MUTED + " " * gap + state_color + right + MUTED + " │")
        else:
            gap = max(1, inner - len(header) - len(right))
            lines.append("│ " + BLUE + header + " " * gap + state_color + right + MUTED + " │")
        if button:
            button_x = 3 + len(header) + gap + len(state) + 2
            config_hitbox = (button_x, button_x + len(button) - 1, len(lines))
        if provider.plan:
            content = f"plan  {provider.plan}"
            lines.append("│ " + TEXT + truncate(content, inner).ljust(inner) + MUTED + " │")
        ordered_windows = sorted(provider.windows, key=lambda window: (0 if window.label == "weekly" else 1 if window.label in ("5 hour", "5 day") else 2, window.label))
        for window in ordered_windows[:2]:
            compact = inner < 35
            reset_width = 5 if compact else 8
            reset_seconds = parse_duration_seconds(window.reset_text) if window.reset_text else (max(0, window.resets_at - int(time.time())) if window.resets_at else None)
            reset = truncate(
                format_countdown(reset_seconds, compact=compact),
                reset_width,
            )
            if compact:
                label = "week" if window.label == "weekly" else ("5h" if window.label == "5 hour" else "5d" if window.label == "5 day" else truncate(window.label, 4))
                bar_width = max(1, inner - 16)
                row = f"{label:<4} {bar(window.used_percent, bar_width)} {window.used_percent:>3}% {MUTED}{reset:>{reset_width}}{RESET}{BG}"
            else:
                bar_width = max(1, inner - 25)
                row = f"{window.label:<8} {bar(window.used_percent, bar_width)}  {window.used_percent:>3}%  {MUTED}{reset:>{reset_width}}{RESET}{BG}"
            lines.append("│ " + TEXT + row + MUTED + " │")
        for detail in provider.details[:2]:
            lines.append("│ " + TEXT + truncate(detail, inner).ljust(inner) + MUTED + " │")
        if provider.error:
            lines.append("│ " + state_color + truncate(provider.error, inner).ljust(inner) + MUTED + " │")
        lines.append("├" + "─" * (width - 2) + "┤")
    footer = "r refresh · s setup · q close" if config_hitbox else "r refresh · q close"
    lines[-1] = "└" + "─" * (width - 2) + "┘"
    if len(lines) < height:
        lines.insert(-1, "│ " + MUTED + footer.ljust(inner) + " │")
    output = "\n".join(lines[:height])
    sys.stdout.write(f"{ESC}H{BG}{output}{RESET}{ESC}J")
    sys.stdout.flush()
    return config_hitbox


def open_deepseek_config() -> None:
    herdr = find_binary("herdr")
    if not herdr:
        return
    subprocess.run(
        [herdr, "plugin", "pane", "open", "--plugin", "herdr.quota", "--entrypoint", "deepseek-config",
         "--placement", "popup", "--width", "68", "--height", "15", "--focus"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def read_input(fd: int) -> tuple[str, tuple[int, int] | None]:
    first = os.read(fd, 1)
    if first != b"\x1b":
        return first.decode("utf-8", "ignore"), None
    data = bytearray(first)
    deadline = time.monotonic() + 0.08
    while time.monotonic() < deadline and len(data) < 48:
        ready, _, _ = select.select([fd], [], [], 0.01)
        if not ready:
            continue
        data.extend(os.read(fd, 16))
        if data.endswith((b"M", b"m")):
            break
    match = re.fullmatch(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", bytes(data))
    if match and match.group(4) == b"M" and int(match.group(1)) == 0:
        return "mouse", (int(match.group(2)), int(match.group(3)))
    return "escape", None


def config_mtime() -> float:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    path = Path(config_dir) / ".env" if config_dir else Path.home() / ".config" / "herdr" / "plugins" / "config" / "herdr.quota" / ".env"
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0


def refresh(store: Store, flag: list[bool]) -> None:
    if flag[0]:
        return
    flag[0] = True
    collectors = (
        codex_collect,
        claude_collect,
        lambda: usage_cli_collect("KIMI", "kimi"),
        deepseek_collect,
    )
    threads = []
    for index, collector in enumerate(collectors):
        thread = threading.Thread(target=lambda fn=collector: store.put(fn()), daemon=True)
        thread.start()
        threads.append(thread)
        if index + 1 < len(collectors):
            time.sleep(COLLECTOR_STAGGER_SECONDS)
    for thread in threads:
        thread.join(timeout=COLLECTOR_JOIN_TIMEOUT_SECONDS)
    flag[0] = False


def diagnose() -> int:
    env = load_plugin_env()
    checks = {
        "codex": find_binary("codex") or "missing",
        "claude": find_binary("claude") or "missing",
        "kimi": find_binary("kimi") or "missing",
        "deepseek_key": "configured" if os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY") else "missing",
        "plugin_config": os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "not provided"),
    }
    print(json.dumps(checks, indent=2))
    return 0 if all(value != "missing" for value in checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    if args.diagnose:
        return diagnose()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("herdr-quota dashboard requires a PTY", file=sys.stderr)
        return 2
    store = Store()
    refreshing = [False]
    thread = threading.Thread(target=refresh, args=(store, refreshing), daemon=True)
    thread.start()
    old = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write(f"{ESC}?1049h{ESC}?25l{ESC}?1000h{ESC}?1006h")
        last_auto = time.monotonic()
        last_config_mtime = config_mtime()
        config_hitbox: tuple[int, int, int] | None = None
        while True:
            config_hitbox = render(store, refreshing[0])
            ready, _, _ = select.select([sys.stdin], [], [], 0.25)
            if ready:
                key, mouse = read_input(sys.stdin.fileno())
                if key in ("q", "Q", "\x03", "\x04"):
                    break
                if key in ("r", "R") and not refreshing[0]:
                    threading.Thread(target=refresh, args=(store, refreshing), daemon=True).start()
                if key in ("s", "S") and config_hitbox:
                    open_deepseek_config()
                if key == "mouse" and mouse and config_hitbox:
                    x_start, x_end, y = config_hitbox
                    if x_start <= mouse[0] <= x_end and mouse[1] == y:
                        open_deepseek_config()
            current_mtime = config_mtime()
            if current_mtime != last_config_mtime:
                last_config_mtime = current_mtime
                if not refreshing[0]:
                    threading.Thread(target=refresh, args=(store, refreshing), daemon=True).start()
            if time.monotonic() - last_auto >= AUTO_REFRESH_SECONDS and not refreshing[0]:
                threading.Thread(target=refresh, args=(store, refreshing), daemon=True).start()
                last_auto = time.monotonic()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        sys.stdout.write(f"{RESET}{ESC}?1000l{ESC}?1006l{ESC}?25h{ESC}?1049l")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
