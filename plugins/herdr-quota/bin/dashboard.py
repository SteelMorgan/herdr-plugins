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
QWEN_WEB_HOME = "https://home.qwencloud.com"
QWEN_WEB_USER_INFO_URL = f"{QWEN_WEB_HOME}/tool/user/info.json"
QWEN_WEB_GATEWAY_URL = f"{QWEN_WEB_HOME}/data/api.json"
QWEN_TEAM_PRODUCT_CODE = "sfm_tokenplanteams_dp_intl"
QWEN_WEB_SESSION_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "qwen-web-session.json"


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
        self.items = {name: Provider(name) for name in ("CODEX", "CLAUDE", "KIMI", "QWEN", "DEEPSEEK API")}

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


def qwen_web_sec_token(cookie: str) -> tuple[str, str | None]:
    """Resolve the console CSRF token; returns (status, sec_token)."""
    request = urllib.request.Request(
        QWEN_WEB_USER_INFO_URL,
        headers={"Cookie": cookie, "User-Agent": "herdr-quota/0.1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            info = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError):
        return "error", None
    if not isinstance(info, dict):
        return "error", None
    sec = (info.get("data") or {}).get("secToken")
    if info.get("code") == "ConsoleNeedLogin" or not isinstance(sec, str) or sec in ("", "0"):
        return "expired", None
    return "ok", sec


def qwen_web_post(cookie: str, sec_token: str, product: str, action: str, params: dict[str, object], region: str, language: str) -> tuple[int, object | None]:
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
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0, None


def _qwen_collect_once(cookie: str) -> Provider:
    provider = Provider("QWEN")
    status, sec = qwen_web_sec_token(cookie)
    if status == "expired":
        provider.status, provider.error = "error", "session expired — run the refresh-qwen action"
        return provider
    if status == "error" or not sec:
        provider.status, provider.error = "error", "qwencloud console unreachable"
        return provider

    code, payload = qwen_web_post(
        cookie, sec, "BssOpenAPI-V3", "GetSeatSubscriptionSummary",
        {"productCode": QWEN_TEAM_PRODUCT_CODE}, "cn-hangzhou", "zh-CN",
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data.get("Data") if isinstance(data, dict) else None
    groups = data.get("SubscriptionGroupList") if isinstance(data, dict) else None
    if code != 200 or not isinstance(data, dict) or not isinstance(groups, list) or not groups:
        provider.status, provider.error = "error", f"seat summary unavailable (HTTP {code})"
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
        window_order = {"monthly": 0, "weekly": 1, "5 hour": 2, "5 day": 3}
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
        kimi_collect,
        qwen_collect,
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
        "qwen_session": "configured" if load_qwen_cookie() else "missing",
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
