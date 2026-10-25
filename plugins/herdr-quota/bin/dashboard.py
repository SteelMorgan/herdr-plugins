#!/usr/bin/python3
"""Dependency-free terminal dashboard for Herdr model quotas."""

from __future__ import annotations

import argparse
import base64
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from usage_export import publish, record_failure


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
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"
KIMI_CODE_USAGE_URL = f"{KIMI_CODE_BASE_URL}/usages"
KIMI_CREDENTIAL_PATHS = (
    Path.home() / ".kimi-code" / "credentials" / "kimi-code.json",
    Path.home() / ".kimi" / "credentials" / "kimi-code.json",
)
KIMI_WEB_BASE_URL = "https://www.kimi.com/apiv2"
KIMI_WEB_AUTH_URL = "https://auth.kimi.com/api/account.gateway.v1.AuthService/RefreshToken"
KIMI_WEB_STATS_URL = f"{KIMI_WEB_BASE_URL}/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
KIMI_WEB_TOKEN_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "kimi-web-token.json"
CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
QWEN_WEB_HOME = "https://home.qwencloud.com"
QWEN_WEB_USER_INFO_URL = f"{QWEN_WEB_HOME}/tool/user/info.json"
QWEN_WEB_GATEWAY_URL = f"{QWEN_WEB_HOME}/data/api.json"
QWEN_TEAM_PRODUCT_CODE = "sfm_tokenplanteams_dp_intl"
QWEN_WEB_SESSION_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "qwen-web-session.json"
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
DSH_CREDENTIALS_PATH = Path.home() / ".dsh" / ".credentials.yaml"
# Z.ai reports each window as a (unit, number) pair. Only units confirmed
# against the live API are mapped; anything else keeps a raw label and no
# duration, so an unknown window never produces a fabricated pace.
ZAI_UNIT_MINUTES = {3: 60, 6: 10080}
ZAI_UNIT_NAMES = {3: "hour", 6: "week"}


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
        # Kimi, GLM and Qwen collectors stay in source but are disabled in the live dashboard.
        self.items = {name: Provider(name) for name in ("CODEX", "CLAUDE", "MIMO", "DEEPSEEK API")}

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


def format_window_reset(window: Window, compact: bool) -> tuple[str, int]:
    """Show the time remaining until any quota window resets."""
    reset_seconds = parse_duration_seconds(window.reset_text) if window.reset_text else (
        max(0, window.resets_at - int(time.time())) if window.resets_at else None
    )
    reset_width = 5 if compact else 8
    return truncate(format_countdown(reset_seconds, compact=compact), reset_width), reset_width


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
    provider = usage_cli_collect("CLAUDE", "claude")
    tier = claude_usage_multiplier()
    if tier:
        provider.plan = f"{provider.plan} {tier}".strip()
    return provider


def claude_usage_multiplier() -> str:
    """Read the supported Claude account tier without exposing credentials."""
    try:
        payload = json.loads(CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""

    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return ""
    raw_tier = oauth.get("rateLimitTier")
    if not isinstance(raw_tier, str):
        return ""

    match = re.search(r"(?i)(?:^|[_-])(?P<suffix>5|20)x(?:$|[_-])", raw_tier)
    if not match:
        match = re.search(r"(?i)(?:^|[_-])x(?P<prefix>5|20)(?:$|[_-])", raw_tier)
    if not match:
        return ""
    multiplier = match.group("suffix") or match.group("prefix")
    return f"{multiplier}x"


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


def parse_quota_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_kimi_total_quota(payload: object) -> Window | None:
    """Convert Kimi's optional totalQuota aggregate into a monthly window."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("totalQuota") or payload.get("total_quota")
    if not isinstance(raw, dict):
        return None

    limit = parse_quota_int(raw.get("limit"))
    remaining = parse_quota_int(raw.get("remaining"))
    used = parse_quota_int(raw.get("used"))
    if limit is None or limit <= 0:
        return None
    if used is None and remaining is not None:
        used = limit - remaining
    if used is None:
        return None

    used = max(0, min(limit, used))
    reset_value = raw.get("resetTime") or raw.get("reset_time")
    reset_text = str(reset_value) if isinstance(reset_value, str) else ""
    return Window(
        "monthly",
        max(0, min(100, round(used * 100 / limit))),
        resets_at=iso_epoch(reset_text),
    )


def parse_quota_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def parse_ratio_percent(value: object) -> int | None:
    ratio = parse_quota_float(value)
    if ratio is None:
        return None
    if 0 <= ratio <= 1:
        ratio *= 100
    if not 0 <= ratio <= 100:
        return None
    return max(0, min(100, round(ratio)))


def parse_kimi_timestamp(value: object) -> int | None:
    if isinstance(value, str):
        return iso_epoch(value)
    if not isinstance(value, dict):
        return None
    for key in ("timestamp", "time", "value"):
        nested = value.get(key)
        if nested is not None:
            parsed = parse_kimi_timestamp(nested)
            if parsed is not None:
                return parsed
    seconds = parse_quota_int(value.get("seconds"))
    return seconds if seconds is not None else None


def parse_kimi_subscription_balance(payload: object) -> Window | None:
    """Convert Kimi web membership stats into the shared monthly window."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("subscriptionBalance") or payload.get("subscription_balance")
    if not isinstance(raw, dict):
        balances = payload.get("balances") or []
        if isinstance(balances, list):
            raw = next((item for item in balances if isinstance(item, dict)), None)
    if not isinstance(raw, dict):
        return None

    used_percent = parse_ratio_percent(raw.get("amountUsedRatio", raw.get("amount_used_ratio")))
    if used_percent is None:
        amount = parse_quota_float(raw.get("amount"))
        amount_left = parse_quota_float(raw.get("amountLeft", raw.get("amount_left")))
        if amount is not None and amount > 0 and amount_left is not None:
            used_percent = max(0, min(100, round((amount - amount_left) * 100 / amount)))
    if used_percent is None:
        return None

    reset_value = raw.get("expireTime", raw.get("expire_time"))
    if reset_value is None:
        upcoming = raw.get("upcomingExpiration", raw.get("upcoming_expiration"))
        if isinstance(upcoming, dict):
            reset_value = upcoming.get("timestamp", upcoming.get("time"))
    return Window("monthly", used_percent, resets_at=parse_kimi_timestamp(reset_value))


def load_kimi_access_token() -> str | None:
    """Read the existing Kimi Code OAuth token without exposing or persisting it."""
    for path in KIMI_CREDENTIAL_PATHS:
        try:
            credentials = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        token = credentials.get("access_token") if isinstance(credentials, dict) else None
        if isinstance(token, str) and token.strip():
            return token
    return None


def kimi_web_token_path() -> Path:
    configured = os.environ.get("KIMI_WEB_TOKEN_FILE")
    return Path(configured) if configured else KIMI_WEB_TOKEN_PATH


def load_kimi_web_tokens() -> tuple[str, str | None] | None:
    path = kimi_web_token_path()
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(credentials, dict):
        return None
    access = credentials.get("access_token") or credentials.get("accessToken")
    refresh = credentials.get("refresh_token") or credentials.get("refreshToken")
    if not isinstance(access, str) or not access.strip():
        return None
    return access, refresh if isinstance(refresh, str) and refresh.strip() else None


def kimi_web_device_id(token: str) -> str | None:
    try:
        payload_part = token.split(".")[1]
        padding = "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part + padding))
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    device_id = payload.get("device_id") if isinstance(payload, dict) else None
    return str(device_id) if device_id else None


def save_kimi_web_tokens(access: str, refresh: str) -> None:
    """Atomically rotate the local cache, keeping the secret out of logs."""
    path = kimi_web_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".kimi-web-token.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(
                {
                    "provider": "kimi-web",
                    "origin": "https://www.kimi.ai",
                    "access_token": access,
                    "refresh_token": refresh,
                    "updated_at": datetime.now().astimezone().isoformat(),
                },
                temporary,
                indent=2,
            )
            temporary.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise


def kimi_web_post(url: str, payload: dict[str, object], token: str | None = None) -> tuple[int, object | None]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "herdr-quota/0.1.0",
        "x-msh-version": "2.0.0",
        "x-msh-platform": "web",
        "X-Language": "en-US",
        "R-Timezone": time.tzname[0] if time.tzname else "UTC",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        device_id = kimi_web_device_id(token)
        if device_id:
            headers["x-msh-device-id"] = device_id
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0, None


def kimi_web_monthly_collect() -> Window | None:
    tokens = load_kimi_web_tokens()
    if tokens is None:
        return None
    access, refresh = tokens
    status, payload = kimi_web_post(KIMI_WEB_STATS_URL, {}, access)
    if status == 401 and refresh:
        refresh_status, refreshed = kimi_web_post(KIMI_WEB_AUTH_URL, {"refresh_token": refresh})
        if refresh_status == 200 and isinstance(refreshed, dict):
            new_access = refreshed.get("accessToken") or refreshed.get("access_token")
            new_refresh = refreshed.get("refreshToken") or refreshed.get("refresh_token") or refresh
            if isinstance(new_access, str) and new_access.strip() and isinstance(new_refresh, str) and new_refresh.strip():
                try:
                    save_kimi_web_tokens(new_access, new_refresh)
                except OSError:
                    pass
                status, payload = kimi_web_post(KIMI_WEB_STATS_URL, {}, new_access)
    if status != 200:
        return None
    return parse_kimi_subscription_balance(payload)


def kimi_monthly_collect(token: str) -> Window | None:
    base_url = os.environ.get("KIMI_CODE_BASE_URL", KIMI_CODE_BASE_URL)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/usages",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "herdr-quota/0.1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return parse_kimi_total_quota(json.load(response))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def kimi_collect() -> Provider:
    provider = usage_cli_collect("KIMI", "kimi")
    monthly = kimi_web_monthly_collect()
    if monthly is None:
        token = load_kimi_access_token()
        monthly = kimi_monthly_collect(token) if token else None
    if monthly is not None:
        provider.windows = [window for window in provider.windows if window.label != "monthly"]
        provider.windows.append(monthly)
        if monthly.used_percent >= 100:
            provider.status, provider.error = "depleted", ""
        elif provider.status in ("empty", "unsupported", "unavailable"):
            provider.status, provider.error = "ok", ""
    return provider


def qwen_web_session_path() -> Path:
    configured = os.environ.get("QWEN_WEB_SESSION_FILE")
    return Path(configured) if configured else QWEN_WEB_SESSION_PATH


def load_qwen_cookie() -> str | None:
    """Read the qwencloud.com session cookie; env/config win over the secret file."""
    cookie = os.environ.get("QWENCLOUD_COOKIES") or load_plugin_env().get("QWENCLOUD_COOKIES")
    if isinstance(cookie, str) and cookie.strip():
        return cookie.strip()
    try:
        session = json.loads(qwen_web_session_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cookie = session.get("cookie") if isinstance(session, dict) else None
    return cookie.strip() if isinstance(cookie, str) and cookie.strip() else None


def qwen_web_sec_token(cookie: str) -> tuple[str, str | None, str]:
    """Resolve the console CSRF token; returns (status, sec_token, cause).

    The cause is carried out verbatim: qwencloud sits behind a gateway that
    intermittently drops TLS connections, and is_transient_error can only
    classify that — and earn a retry — if the original text survives.
    """
    request = urllib.request.Request(
        QWEN_WEB_USER_INFO_URL,
        headers={"Cookie": cookie, "User-Agent": "herdr-quota/0.1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            info = json.load(response)
    except urllib.error.HTTPError as exc:
        return "error", None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return "error", None, str(exc)
    if not isinstance(info, dict):
        return "error", None, "malformed console response"
    sec = (info.get("data") or {}).get("secToken")
    if info.get("code") == "ConsoleNeedLogin" or not isinstance(sec, str) or sec in ("", "0"):
        return "expired", None, ""
    return "ok", sec, ""


def qwen_web_post(cookie: str, sec_token: str, product: str, action: str, params: dict[str, object], region: str, language: str) -> tuple[int, object | None, str]:
    body = urllib.parse.urlencode({
        "product": product,
        "action": action,
        "sec_token": sec_token,
        "region": region,
        "language": language,
        "params": json.dumps(params),
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{QWEN_WEB_GATEWAY_URL}?product={product}&action={action}",
        data=body,
        headers={
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "herdr-quota/0.1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.status, json.load(response), ""
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return 0, None, str(exc)


def _qwen_collect_once(cookie: str) -> Provider:
    provider = Provider("QWEN")
    status, sec, cause = qwen_web_sec_token(cookie)
    if status == "expired":
        provider.status, provider.error = "error", "session expired — run the refresh-qwen action"
        return provider
    if status == "error" or not sec:
        provider.status = "error"
        provider.error = f"qwencloud console unreachable: {cause}" if cause else "qwencloud console unreachable"
        return provider

    code, payload, cause = qwen_web_post(
        cookie, sec, "BssOpenAPI-V3", "GetSeatSubscriptionSummary",
        {"productCode": QWEN_TEAM_PRODUCT_CODE}, "cn-hangzhou", "zh-CN",
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data.get("Data") if isinstance(data, dict) else None
    groups = data.get("SubscriptionGroupList") if isinstance(data, dict) else None
    if code != 200 or not isinstance(data, dict) or not isinstance(groups, list) or not groups:
        provider.status = "error"
        provider.error = "seat summary unavailable ({})".format(cause or f"HTTP {code}")
        return provider

    total = surplus = 0.0
    resets_at = 0
    specs: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        spec = str(group.get("SpecType") or "").lower()
        if spec:
            specs.append(spec)
        resets_at = max(resets_at, parse_quota_int(group.get("NextCycleFlushTime")) or parse_quota_int(data.get("EndTime")) or 0)
        for equity in group.get("EquityList") or []:
            if isinstance(equity, dict):
                total += parse_quota_float(equity.get("TotalValue")) or 0
                surplus += parse_quota_float(equity.get("SurplusValue")) or 0
    start_ms = parse_quota_int(data.get("StartTime")) or 0
    if total > 0 and resets_at > start_ms:
        used = max(0, min(100, round((total - surplus) / total * 100)))
        provider.windows.append(Window("monthly", used, resets_at // 1000, (resets_at - start_ms) // 60000))
    provider.plan = f"token plan {'/'.join(specs)} · {total - surplus:,.0f}/{total:,.0f} credits"

    if not provider.windows:
        provider.status, provider.error = "empty", "no token-plan subscription"
    elif provider.windows[0].used_percent >= 100:
        provider.status = "depleted"
    else:
        provider.status = "ok"
    return provider


def qwen_collect() -> Provider:
    cookie = load_qwen_cookie()
    if not cookie:
        return Provider("QWEN", status="unconfigured", error="run the refresh-qwen action and log in")
    return collect_with_retry(lambda: _qwen_collect_once(cookie))


def plugin_config_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".env"
    return Path.home() / ".config" / "herdr" / "plugins" / "config" / "herdr.quota" / ".env"


def load_plugin_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = plugin_config_path()
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


def load_zai_key() -> str | None:
    """Prefer explicit plugin config, then the key DSH already uses for Z.ai."""
    token = os.environ.get("ZAI_API_KEY") or load_plugin_env().get("ZAI_API_KEY")
    if token:
        return token
    try:
        for raw in DSH_CREDENTIALS_PATH.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s+ZAI_API_KEY:\s*(\S+)", raw)
            if match:
                return match.group(1).strip("\"'")
    except OSError:
        pass
    return None


def zai_window_label(unit: object, number: object) -> tuple[str, int | None]:
    """Map a Z.ai (unit, number) pair onto a dashboard label and duration."""
    unit_minutes = ZAI_UNIT_MINUTES.get(unit) if isinstance(unit, int) else None
    name = ZAI_UNIT_NAMES.get(unit) if isinstance(unit, int) else None
    count = number if isinstance(number, int) and number > 0 else None
    if not name or not unit_minutes or count is None:
        return "quota", None
    if name == "week" and count == 1:
        return "weekly", unit_minutes
    return f"{count} {name}", unit_minutes * count


def parse_zai_percent(item: dict) -> int | None:
    """Z.ai reports whole percents, so the 0-1 ratio heuristic must not apply."""
    reported = parse_quota_float(item.get("percentage"))
    if reported is not None and 0 <= reported <= 100:
        return max(0, min(100, round(reported)))
    used = parse_quota_int(item.get("currentValue"))
    total = parse_quota_int(item.get("usage"))
    if used is None or not total or total <= 0:
        return None
    return max(0, min(100, round(used / total * 100)))


def parse_zai_limit(item: object) -> Window | None:
    """Return the quota window for one Z.ai limit entry, or None when unusable."""
    if not isinstance(item, dict):
        return None
    percent = parse_zai_percent(item)
    if percent is None:
        return None
    label, duration = zai_window_label(item.get("unit"), item.get("number"))
    if item.get("type") == "TIME_LIMIT" and label == "quota":
        label = "monthly"
    reset = item.get("nextResetTime")
    resets_at = int(reset) // 1000 if isinstance(reset, (int, float)) and reset > 0 else None
    return Window(label, percent, resets_at=resets_at, duration_minutes=duration)


def _glm_collect_once(token: str) -> Provider:
    provider = Provider("GLM")
    request = urllib.request.Request(
        ZAI_QUOTA_URL,
        headers={
            "Authorization": token,
            "Accept": "application/json",
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
            "User-Agent": "herdr-quota/0.3.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return Provider("GLM", status="error", error=f"HTTP {exc.code}")
    except Exception as exc:
        return Provider("GLM", status="error", error=str(exc))
    if not isinstance(payload, dict) or not payload.get("success"):
        return Provider("GLM", status="error", error="quota response rejected")
    data = payload.get("data")
    if not isinstance(data, dict):
        return Provider("GLM", status="error", error="quota response malformed")
    level = data.get("level")
    if isinstance(level, str) and level.strip():
        provider.plan = f"coding plan {level.strip()}"
    limits = data.get("limits")
    for item in limits if isinstance(limits, list) else []:
        window = parse_zai_limit(item)
        if window:
            provider.windows.append(window)
    if not provider.windows:
        provider.status = "empty"
        provider.error = "no quota windows reported"
        return provider
    provider.status = "depleted" if any(w.used_percent >= 100 for w in provider.windows) else "ok"
    return provider


def glm_collect() -> Provider:
    token = load_zai_key()
    if not token:
        return Provider(
            "GLM",
            status="unconfigured",
            error="set ZAI_API_KEY in plugin config .env",
        )
    return collect_with_retry(lambda: _glm_collect_once(token))


def load_mimo_key() -> str | None:
    """Prefer plugin .env, then the MiMoCode auth store (API key, never logged)."""
    token = os.environ.get("MIMO_API_KEY") or load_plugin_env().get("MIMO_API_KEY")
    if token:
        return token
    try:
        payload = json.loads((Path.home() / ".local" / "share" / "mimocode" / "auth.json").read_text(encoding="utf-8"))
        entry = payload.get("xiaomi") or {}
        key = entry.get("key")
        return key if isinstance(key, str) and key.strip() else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


MIMO_WEB_SESSION_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "mimo-web-session.json"
MIMO_WEB_API = "https://platform.xiaomimimo.com/api/v1"
# Console cookie endpoints — cache briefly so rapid refreshes do not hammer them.
_MIMO_WEB_CACHE: dict[str, tuple[float, object]] = {}
_MIMO_WEB_MIN_INTERVAL_SECONDS = 60.0


def load_mimo_web_cookies() -> str | None:
    """Browser Cookie header for platform.xiaomimimo.com (account session)."""
    raw = os.environ.get("MIMO_WEB_COOKIES") or load_plugin_env().get("MIMO_WEB_COOKIES")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    try:
        session = json.loads(MIMO_WEB_SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(session, dict):
        return None
    cookie = session.get("cookie")
    return cookie.strip() if isinstance(cookie, str) and cookie.strip() else None


def _mimo_web_get(path: str, cookie: str) -> object | None:
    # Dashboard auto-refresh is 5 min; still guard bursts (manual `r` spam).
    now = time.time()
    cache_key = path
    cached = _MIMO_WEB_CACHE.get(cache_key)
    if cached and now - cached[0] < _MIMO_WEB_MIN_INTERVAL_SECONDS:
        return cached[1]
    request = urllib.request.Request(
        MIMO_WEB_API + path,
        headers={
            "Cookie": cookie,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) herdr-quota/0.1.0",
            "Referer": "https://platform.xiaomimimo.com/console/usage",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.load(response)
    except Exception:
        return None
    _MIMO_WEB_CACHE[cache_key] = (now, payload)
    return payload


_MIMO_REFRESH_ATTEMPT = 0.0


def mimo_try_auto_refresh() -> bool:
    """Renew console cookies from the persistent Chrome/Brave profile (CDP).

    serviceToken lives ~6h; the API wipes slh/ph on 401, so renewal must use
    the browser profile's long-lived passToken — never Playwright (Google OAuth).
    """
    global _MIMO_REFRESH_ATTEMPT
    now = time.time()
    if now - _MIMO_REFRESH_ATTEMPT < 1800:  # at most every 30 min
        return False
    _MIMO_REFRESH_ATTEMPT = now
    script = Path(__file__).resolve().parent / "refresh-mimo-cookies.sh"
    if not script.is_file():
        return False
    try:
        result = subprocess.run(
            ["/bin/sh", str(script), "--auto"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _mimo_find_used_limit(payload: object) -> tuple[float | None, float | None, int | None]:
    """Prefer monthUsage/plan_total_token from tokenPlan/usage, else walk the tree."""
    used = limit = reset = None

    def take(obj: object) -> None:
        nonlocal used, limit, reset
        if not isinstance(obj, dict):
            return
        u = parse_quota_float(obj.get("used"))
        lim = parse_quota_float(obj.get("limit"))
        if u is not None and lim:
            used, limit = u, lim

    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        month = data.get("monthUsage") if isinstance(data.get("monthUsage"), dict) else None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        for block in (month, usage):
            if not isinstance(block, dict):
                continue
            for item in block.get("items") or []:
                if isinstance(item, dict) and str(item.get("name") or "").endswith("total_token"):
                    take(item)
                    if used is not None and limit:
                        return used, limit, reset
            if used is None:
                # percent-only shape
                pct = parse_quota_float(block.get("percent"))
                if pct is not None:
                    # API returns 0.0157 for 1.57% or 2 for 2% depending on field
                    used = pct * 100.0 if pct <= 1 else pct
                    limit = None
                    return used, limit, reset

    def walk(obj: object) -> None:
        nonlocal used, limit, reset
        if isinstance(obj, dict):
            keys = {str(k).lower(): k for k in obj}
            u = None
            lim = None
            for cand in ("usedtokens", "used_tokens", "usedtoken", "used", "consumed", "usedcount"):
                if cand in keys and u is None:
                    u = parse_quota_float(obj[keys[cand]])
            for cand in ("limittokens", "limit_tokens", "totaltokens", "total_tokens", "quota", "limit", "total"):
                if cand in keys and lim is None:
                    lim = parse_quota_float(obj[keys[cand]])
            if u is not None and lim:
                used, limit = u, lim
            for cand in ("resetat", "reset_at", "expiresat", "expires_at", "reset"):
                if cand in keys and reset is None:
                    reset = parse_quota_int(obj[keys[cand]])
                    if reset and reset > 10**12:
                        reset = reset // 1000
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    return used, limit, reset


def month_window_bounds(now: float | None = None) -> tuple[int, int]:
    from datetime import datetime

    dt = datetime.fromtimestamp(now or time.time()).astimezone()
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp()), int(end.timestamp())


def week_window_bounds(now: float | None = None) -> tuple[int, int]:
    from datetime import datetime, timedelta

    dt = datetime.fromtimestamp(now or time.time()).astimezone()
    start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return int(start.timestamp()), int(end.timestamp())


def _mimo_used_percent(env: dict[str, str]) -> tuple[float | None, str]:
    raw = env.get("MIMO_MONTHLY_USED_PERCENT") or os.environ.get("MIMO_MONTHLY_USED_PERCENT")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0.0, min(100.0, float(raw))), "config used-percent"
        except ValueError:
            pass
    used = env.get("MIMO_MONTHLY_USED") or os.environ.get("MIMO_MONTHLY_USED")
    limit = env.get("MIMO_MONTHLY_LIMIT") or os.environ.get("MIMO_MONTHLY_LIMIT")
    if used is not None and limit is not None:
        try:
            u, lim = float(used), float(limit)
            if lim > 0:
                return max(0.0, min(100.0, u / lim * 100.0)), "config used/limit"
        except ValueError:
            pass
    return None, ""


def _mimo_fetch_quota(key: str) -> tuple[float | None, float | None, int | None]:
    """Best-effort live quota (used%, limit, resets_at)."""
    bases = (
        "https://token-plan-sgp.xiaomimimo.com",
        "https://platform.xiaomimimo.com",
    )
    paths = ("/v1/usage", "/v1/quota", "/api/v1/usage", "/api/v1/token-plan/usage")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    for base in bases:
        for path in paths:
            request = urllib.request.Request(base + path, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=6) as response:
                    payload = json.load(response)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            used = parse_quota_float(data.get("used") or data.get("used_percent") or data.get("usedPercent"))
            limit = parse_quota_float(data.get("limit") or data.get("total") or data.get("quota"))
            reset = parse_quota_int(data.get("resets_at") or data.get("reset_at") or data.get("expires_at"))
            if used is not None and limit:
                if used > 100 and limit:
                    used = used / limit * 100.0
                return used, limit, reset
            if used is not None:
                return used, None, reset
    return None, None, None



def mimo_parse_period_end(payload: object) -> int | None:
    """currentPeriodEnd from tokenPlan/detail → unix seconds (local)."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw = data.get("currentPeriodEnd") or data.get("current_period_end") or data.get("periodEnd")
    if not isinstance(raw, str) or not raw.strip():
        return None
    from datetime import datetime
    text = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


def mimo_billing_window(period_end_ts: int) -> tuple[int, int]:
    """
    Billing month ≈ [end − 1 calendar month 00:00, end].
    Period is anchored on the subscription day (e.g. 5th), not the 1st.
    """
    from datetime import datetime
    end = datetime.fromtimestamp(period_end_ts)
    if end.month == 1:
        start = end.replace(year=end.year - 1, month=12, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = end.replace(month=end.month - 1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), period_end_ts


def mimo_week_index(period_start_ts: int, now: float | None = None) -> tuple[int, int, int]:
    """0-based completed weeks, current week 1-based, week_start ts."""
    now_ts = int(now or time.time())
    elapsed = max(0, now_ts - period_start_ts)
    completed = elapsed // (7 * 24 * 3600)
    week_start = period_start_ts + completed * 7 * 24 * 3600
    return int(completed), int(completed) + 1, week_start


def mimo_week_state_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "mimo-week-state.json"
    return Path.home() / ".config" / "herdr" / "plugins" / "config" / "herdr.quota" / "mimo-week-state.json"


def _mimo_last_period_end() -> int | None:
    try:
        state = json.loads(mimo_week_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parse_quota_int(state.get("period_end_ts"))


def _mimo_save_env_snapshot(used_abs: float, limit: float) -> None:
    """Keep .env fallback in sync so a dead cookie still shows recent numbers."""
    path = plugin_config_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return
    out: list[str] = []
    seen_used = seen_limit = False
    for line in lines:
        if line.startswith("MIMO_MONTHLY_USED="):
            out.append(f"MIMO_MONTHLY_USED={int(used_abs)}")
            seen_used = True
        elif line.startswith("MIMO_MONTHLY_LIMIT="):
            out.append(f"MIMO_MONTHLY_LIMIT={int(limit)}")
            seen_limit = True
        elif line.startswith("MIMO_MONTHLY_USED_PERCENT="):
            continue
        else:
            out.append(line)
    if not seen_used:
        out.append(f"MIMO_MONTHLY_USED={int(used_abs)}")
    if not seen_limit:
        out.append(f"MIMO_MONTHLY_LIMIT={int(limit)}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


def mimo_week_percent(used_abs: float, limit: float, period_end_ts: int | None = None, now: float | None = None) -> tuple[int, str]:
    """
    Weekly bar against this week's *effective* budget (limit/4).

    Pool model (billing cycle, not calendar month):
      W = limit/4
      carry = net credit at end of previous week (+ under-spend, − over-spend)
      budget   = W + max(0, carry)          # unused rolls forward (bigger base)
      consumed = week_spent + max(0, -carry)  # debt starts the week pre-spent
      week%    = consumed / budget * 100    # may exceed 100

    Week 1 spent 30B of W=20.5B → 146%. Week 2 then starts at 46% (9.5B debt).
    Week 1 spent 10B → week 2 budget is 31B (unused 10.5B rolled).
    """
    if not limit or limit <= 0:
        return 0, "no-limit"
    W = limit / 4.0
    now_ts = now or time.time()

    if period_end_ts:
        period_start, _period_end = mimo_billing_window(period_end_ts)
    else:
        period_start, _ = month_window_bounds(now)

    completed, week_no, _week_start_ts = mimo_week_index(period_start, now_ts)
    path = mimo_week_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    prev_period = parse_quota_int(state.get("period_end_ts"))
    prev_week = parse_quota_int(state.get("week_no"))
    last_used = parse_quota_float(state.get("last_used"))
    week_start_used = parse_quota_float(state.get("week_start_used"))
    carry = parse_quota_float(state.get("carry")) or 0.0

    period_changed = bool(prev_period and period_end_ts and prev_period != period_end_ts)
    reset_period = period_changed or (last_used is not None and used_abs + 1 < last_used)

    if reset_period:
        carry = 0.0
        week_start_used = used_abs  # spend before this week does not count
        prev_week = week_no
    elif week_start_used is None:
        # First sample ever: week 1 — all spend so far is this week.
        # Later weeks: do not invent week history; start the week at current total
        # only if we have no prior sample (else keep last_used as baseline).
        if week_no <= 1:
            week_start_used = 0.0
        else:
            week_start_used = last_used if last_used is not None else 0.0

    # Close week(s) that ended since the last sample.
    if prev_week is not None and week_no > prev_week:
        closed_spent = max(0.0, used_abs - (week_start_used if week_start_used is not None else 0.0))
        # One W per closed week (assume only the latest boundary matters if we missed samples).
        weeks_closed = max(1, week_no - prev_week)
        # Grant W for each closed week against that week's spend (approximate with
        # all closed_spent attributed to the last closed week — good enough if
        # samples are frequent).
        carry = carry + W * weeks_closed - closed_spent
        week_start_used = used_abs

    if week_start_used is None:
        week_start_used = 0.0

    week_spent = max(0.0, used_abs - week_start_used)
    budget = W + max(0.0, carry)
    consumed = week_spent + max(0.0, -carry)
    percent = int(round(consumed / budget * 100.0)) if budget > 0 else 0
    method = f"w{week_no} carry={carry/1e9:.2f}B spent={week_spent/1e9:.2f}B"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "period_end_ts": period_end_ts if period_end_ts is not None else prev_period,
                    "period_start_ts": period_start,
                    "week_no": week_no,
                    "week_start_used": week_start_used,
                    "last_used": used_abs,
                    "carry": carry,
                    "week_spent": week_spent,
                    "week_consumed": consumed,
                    "week_budget": budget,
                    "updated_at": time.time(),
                    "method": method,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
    except OSError:
        pass
    return percent, method


def mimo_collect() -> Provider:
    """MiMo Token Plan is monthly; weekly row uses month/4 for habit."""
    provider = Provider("MIMO")
    env = load_plugin_env()
    key = load_mimo_key()
    cookies = load_mimo_web_cookies()
    if not cookies and not key and not env.get("MIMO_MONTHLY_USED_PERCENT") and not env.get("MIMO_MONTHLY_LIMIT"):
        provider.status = "unconfigured"
        provider.error = "run capture-mimo-cookies or set MIMO_MONTHLY_* in .env"
        return provider

    used = None
    used_abs = None
    source = ""
    limit = None
    resets_at = None

    # Live console session (cookie) is the primary source — tokens move continuously.
    period_end_ts = None
    if cookies:
        usage_payload = _mimo_web_get("/tokenPlan/usage", cookies)
        detail_payload = _mimo_web_get("/tokenPlan/detail", cookies)
        if usage_payload is None and detail_payload is None:
            # Session dead or network blip — try a silent profile refresh once.
            if mimo_try_auto_refresh():
                cookies = load_mimo_web_cookies()
                _MIMO_WEB_CACHE.clear()
                if cookies:
                    usage_payload = _mimo_web_get("/tokenPlan/usage", cookies)
                    detail_payload = _mimo_web_get("/tokenPlan/detail", cookies)
        if detail_payload is not None:
            period_end_ts = mimo_parse_period_end(detail_payload)
        if usage_payload is not None:
            live_used, live_limit, live_reset = _mimo_find_used_limit(usage_payload)
            if live_used is not None and live_limit:
                used_abs = float(live_used)
                used = max(0.0, min(100.0, live_used / live_limit * 100.0))
                limit = live_limit
                resets_at = live_reset or period_end_ts
                source = "web"
            elif live_used is not None and live_used <= 100 and live_limit is None:
                used = max(0.0, min(100.0, float(live_used)))
                source = "web%"
    # Keep the last known billing window — a 401 must not drop us onto calendar month.
    if period_end_ts is None:
        period_end_ts = parse_quota_int(_mimo_last_period_end())

    if used is None:
        used, cfg_src = _mimo_used_percent(env)
        if used is not None:
            source = cfg_src
    if used is None and key:
        live_used, live_limit, live_reset = _mimo_fetch_quota(key)
        if live_used is not None:
            used = max(0.0, min(100.0, float(live_used)))
            limit = live_limit
            resets_at = live_reset
            source = "api"
    if used is None:
        provider.status = "unconfigured" if not cookies else "error"
        provider.error = (
            "session expired — run refresh-mimo-cookies (Google OAuth in the opened Chrome)"
            if cookies
            else "run refresh-mimo-cookies or set MIMO_MONTHLY_* in .env"
        )
        return provider

    used_i = int(round(used))
    if limit is None:
        limit_raw = env.get("MIMO_MONTHLY_LIMIT") or os.environ.get("MIMO_MONTHLY_LIMIT")
        if limit_raw:
            limit = parse_quota_float(limit_raw)
    if used_abs is None and limit is not None:
        used_abs = (used / 100.0) * float(limit)
    if source == "web" and used_abs is not None and limit:
        _mimo_save_env_snapshot(used_abs, float(limit))
    month_start, month_end = month_window_bounds()
    week_start, week_end = week_window_bounds()
    if resets_at is None:
        resets_at = month_end

    # weekly % vs this week's effective budget (limit/4 + carry/−debt).
    if used_abs is not None and limit:
        week_pct, week_method = mimo_week_percent(used_abs, float(limit), period_end_ts)
    else:
        week_pct, week_method = used_i, "monthly"
    # Billing-week reset when we know the period; else ISO week.
    if period_end_ts:
        _ps, week_no_cur = mimo_billing_window(period_end_ts)[0], mimo_week_index(mimo_billing_window(period_end_ts)[0])[1]
        _completed, _wn, w_start = mimo_week_index(mimo_billing_window(period_end_ts)[0])
        w_end = w_start + 7 * 24 * 3600
        week_reset_ts = w_end
        week_dur = 7 * 24 * 60
    else:
        week_reset_ts = week_end
        week_dur = (week_end - week_start) // 60
    provider.windows.append(Window("weekly", week_pct, week_reset_ts, week_dur))
    provider.windows.append(Window("monthly", used_i, resets_at or (period_end_ts or month_end), (month_end - month_start) // 60))
    if limit is not None:
        def _fmt_tokens(n: float) -> str:
            if n >= 1e9:
                v = n / 1e9
                return f"{v:.2f}B" if abs(v - round(v)) >= 0.01 else f"{v:.0f}B"
            if n >= 1e6:
                v = n / 1e6
                return f"{v:.1f}M" if abs(v - round(v)) >= 0.01 else f"{v:.0f}M"
            if n >= 1e3:
                return f"{n / 1e3:.1f}K"
            return f"{n:g}"

        week_limit = limit / 4.0
        used_abs = (used / 100.0) * limit
        # Compact: fits the dashboard row without truncation. Week = month/4 is
        # the weekly window below; only the numbers live on this line.
        provider.plan = f"{_fmt_tokens(used_abs)}/{_fmt_tokens(limit)} · week {_fmt_tokens(week_limit)}"
    else:
        tag = f" · {source}" if source else ""
        provider.plan = f"token plan · weekly budget = monthly/4{tag}"
    provider.status = "depleted" if used_i >= 100 else "ok"
    return provider


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
        head = "spent · pace"
        right = "updating…" if refreshing else f"updated {stamp}"
    else:
        head = "spent"
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
        pace_window = next(
            (window for window in provider.windows if window.label == "weekly"),
            next((window for window in provider.windows if window.label == "monthly"), None),
        )
        if pace_window:
            pace = format_burn(pace_window)
            pace_rate = burn_rate(pace_window)
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
        # Weekly first (habit); MiMo weekly is derived as month/4.
        window_order = {"weekly": 0, "monthly": 1, "5 hour": 2, "5 day": 3}
        ordered_windows = sorted(provider.windows, key=lambda window: (window_order.get(window.label, 4), window.label))
        for window in ordered_windows[:3]:
            compact = inner < 35
            reset, reset_width = format_window_reset(window, compact)
            if compact:
                label = "week" if window.label == "weekly" else ("mo" if window.label == "monthly" else ("5h" if window.label == "5 hour" else "5d" if window.label == "5 day" else truncate(window.label, 4)))
                bar_width = max(1, inner - 11 - reset_width)
                row = f"{label:<4} {bar(window.used_percent, bar_width)} {window.used_percent:>3}% {MUTED}{reset:>{reset_width}}{RESET}{BG}"
            else:
                bar_width = max(1, inner - 17 - reset_width)
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
    path = plugin_config_path()
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
        mimo_collect,
        # kimi_collect,  # Disabled while this subscription is not monitored.
        # qwen_collect,  # Intentionally disabled; keep the implementation for a possible re-enable.
        # glm_collect,  # Disabled while this subscription is not monitored.
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
    try:
        publish(store.snapshot())
    except Exception:
        # Export failures must not stop quota collection or expose raw errors.
        record_failure()
    flag[0] = False


def diagnose() -> int:
    env = load_plugin_env()
    checks = {
        "codex": find_binary("codex") or "missing",
        "claude": find_binary("claude") or "missing",
        "mimo_cookie": "configured" if load_mimo_web_cookies() else "missing",
        "mimo_key": "configured" if (os.environ.get("MIMO_API_KEY") or load_plugin_env().get("MIMO_API_KEY") or load_mimo_key()) else "missing",
        "mimo_monthly": load_plugin_env().get("MIMO_MONTHLY_USED_PERCENT") or os.environ.get("MIMO_MONTHLY_USED_PERCENT") or "unset",
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
