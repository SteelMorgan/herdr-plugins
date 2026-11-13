#!/usr/bin/env python3
"""Watch Codex and DSH TUI terminals and retry retryable turn failures.

Herdr App instance of herdr-capacity-guard. The retry state machine, error
recognition and cooldown logic are unchanged from the TUI plugin; only the
transport differs:

* TUI ``agent.list`` / socket API      -> ``hc ls --json`` (local + machines)
* TUI ``pane.read`` (recent, ansi)     -> ``hc read <t> --lines N``
* TUI ``agent.prompt`` / mirror route  -> ``hc send <t> "Продолжить"``
  (remote machines are native ``machine/tN`` addresses — no mirror fallback)
* TUI ``events.subscribe``             -> ``hc events --json`` stream
* TUI ``pane.report_metadata`` /
  ``notification.show``                -> ``herdr.guard`` terminal label +
  ``attention.json`` in the state dir, surfaced by the herdr-plugins mod
* fcntl / /proc / SIGTERM              -> cross-platform lock / stop file

Run it on ONE central machine (the one whose core sees the agents); it watches
every terminal that core lists, including managed machines.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_LIB = Path(os.environ.get("HERDR_APP_PLUGINS_ROOT", str(Path(__file__).resolve().parents[3]))) / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import herdr_app  # noqa: E402

CAPACITY_ERROR = "Selected model is at capacity. Please try a different model."
COMPACT_STREAM_ERROR = "Error running remote compact task: stream disconnected before completion:"
RETRY_ERRORS = (CAPACITY_ERROR, COMPACT_STREAM_ERROR)
TURN_ERROR_MARKER = "turn error"
TURN_ERROR_DETAIL = "The engine is currently overloaded"
# Provider-side turn failures render like
# ``turn error · kimi-token-plan API error (400): {...}``; they are transient
# for the retry flow just like the overloaded-engine divider.
TURN_ERROR_API_ERROR = re.compile(r"apierror\(\d{3}\):")
DSH_TUI_AGENT = "dsh-tui"
WATCHED_AGENTS = ("codex", DSH_TUI_AGENT)
RETRY_TEXT = "Продолжить"
RETRY_DELAY_SECONDS = 10 * 60
PLUGIN_SOURCE = "user:herdr-app-agent-guard"
GUARD_LABEL = "herdr.guard"
PANE_STATES_FILE = "pane-states.json"
ATTENTION_FILE = "attention.json"
RECENT_MESSAGES = 8
RECENT_LINES = 240
SCAN_INTERVAL_SECONDS = 5
READY_STATUSES = {"idle", "done"}

STOP = threading.Event()
WATCHING: set[str] = set()
WATCHING_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
PANE_LOCKS: dict[str, threading.Lock] = {}
PANE_LOCKS_LOCK = threading.Lock()
AGENTS_CACHE: list[dict[str, Any]] = []
AGENTS_CACHE_LOCK = threading.Lock()
AGENTS_CACHE_AT = 0.0
CURRENT_EVENTS_PROCESS: subprocess.Popen | None = None


def state_dir() -> Path:
    return herdr_app.state_dir("herdr-agent-guard")


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------
# Transport: hc instead of the TUI socket API


def hc_cli(*args: str, timeout: float = 15) -> Any:
    return herdr_app.hc(*args, timeout=timeout)


def agents_snapshot() -> list[dict[str, Any]]:
    """TUI-shaped agent records from ``hc ls --json``.

    Keeping the TUI record shape (pane_id / agent / agent_status) lets the
    whole recognition and retry state machine run unchanged. ``pane_id`` is
    now the terminal address (``t7`` or ``dev/t3``).

    hc lists terminals in two shapes: compact (``agent`` = reported app,
    ``status`` = state word) and full (``status`` = {app, state, title});
    both are normalized here.
    """
    global AGENTS_CACHE, AGENTS_CACHE_AT
    try:
        terminals = hc_cli("ls")["terminals"]
    except Exception:
        with AGENTS_CACHE_LOCK:
            if time.monotonic() - AGENTS_CACHE_AT < 10:
                return list(AGENTS_CACHE)
        raise
    agents = []
    for terminal in terminals:
        if terminal.get("state") != "live":
            continue
        status = terminal.get("status")
        if isinstance(status, dict):
            app = status.get("app")
            state = status.get("state")
        else:
            # Compact records carry the program name in `agent` when nothing
            # is reported; only a status word proves an actual agent.
            if status is None:
                continue
            app = terminal.get("agent")
            state = status
        if not app:
            continue
        agents.append(
            {
                "pane_id": str(terminal.get("address") or ""),
                "agent": str(app),
                "agent_status": str(state or ""),
            }
        )
    with AGENTS_CACHE_LOCK:
        AGENTS_CACHE = agents
        AGENTS_CACHE_AT = time.monotonic()
    return list(agents)


def agent_kind(agent: dict[str, Any]) -> str:
    return str(agent.get("agent") or "").casefold()


def agent_is_codex(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) == "codex"


def agent_is_dsh_tui(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) == DSH_TUI_AGENT


def agent_is_watched(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) in WATCHED_AGENTS


def agent_for_pane(pane_id: str) -> dict[str, Any] | None:
    """Return the watched agent occupying this terminal, if there is exactly one."""
    matches = [
        agent
        for agent in agents_snapshot()
        if str(agent.get("pane_id") or "") == pane_id and agent_is_watched(agent)
    ]
    return matches[0] if len(matches) == 1 else None


def kind_label(kind: str) -> str:
    return "DSH TUI" if kind == DSH_TUI_AGENT else "Codex"


def read_pane_text(pane_id: str) -> str:
    # hc read returns the screen plus history as text; while the agent is
    # working it returns the screen only, and the guard inspects ready agents.
    text = herdr_app.hc_text("read", pane_id, "--lines", str(RECENT_LINES), timeout=20)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")


def send_continuation(pane_id: str, expected_kind: str = "codex") -> str:
    """Submit the retry through hc. Remote machines are native addresses.

    The TUI version fell back to a Docker mirror route when agent.prompt
    reported agent_not_ready; under the App the same ``hc send`` reaches the
    remote core directly, so one delivery path covers both cases.
    """
    agent = agent_for_pane(pane_id)
    if agent is None or agent_kind(agent) != expected_kind:
        raise RuntimeError(
            f"Herdr no longer recognizes {kind_label(expected_kind)} in {pane_id}"
        )
    if str(agent.get("agent_status") or "") not in READY_STATUSES:
        raise RuntimeError(f"agent not ready in {pane_id}: {agent.get('agent_status')}")
    hc_cli("send", pane_id, RETRY_TEXT, timeout=90)
    return "hc send"


# --------------------------------------------------------------------------
# Attention: terminal label + attention.json (the mod renders/toasts it)


def _load_attention(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_attention(entries: dict[str, dict[str, Any]]) -> None:
    path = state_dir() / ATTENTION_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _set_guard_label(pane_id: str, value: str | None) -> None:
    try:
        if value is None:
            hc_cli("label", pane_id, f"{GUARD_LABEL}=", timeout=10)
        else:
            hc_cli("label", pane_id, f"{GUARD_LABEL}={value}", timeout=10)
    except Exception as exc:
        log(f"could not label terminal {pane_id}: {exc}")


def report_attention(
    pane_id: str,
    reason: str,
    ttl_ms: int | None = None,
    notify: bool = True,
    label: str = "модель перегружена",
    agent_kind: str = "codex",
) -> None:
    now = time.time()
    entry = {
        "reason": reason,
        "label": label,
        "agent": agent_kind,
        "source": PLUGIN_SOURCE,
        "notify": bool(notify),
        "created_at": now,
        "expires_at": now + ttl_ms / 1000 if ttl_ms is not None else None,
    }
    with STATE_LOCK:
        entries = _load_attention(state_dir() / ATTENTION_FILE)
        entries[pane_id] = entry
        _save_attention(entries)
    _set_guard_label(pane_id, label)


def clear_attention(pane_id: str, agent_kind: str = "codex") -> None:
    with STATE_LOCK:
        entries = _load_attention(state_dir() / ATTENTION_FILE)
        if pane_id in entries:
            entries.pop(pane_id, None)
            _save_attention(entries)
    _set_guard_label(pane_id, None)


# --------------------------------------------------------------------------
# Pane state persistence (unchanged from the TUI plugin)


def start_pane_watcher(pane_id: str) -> None:
    """Register a terminal for inspection.

    The TUI kept a per-pane socket subscription for output_matched events.
    hc has no output matching; detection runs on status events plus the
    periodic SCAN_INTERVAL_SECONDS scan, so registration is bookkeeping.
    """
    if not pane_id:
        return
    with WATCHING_LOCK:
        if pane_id in WATCHING:
            return
        WATCHING.add(pane_id)
    log(f"watching agent terminal {pane_id}")


def _load_pane_states(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = None
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(value, dict):
        states = {
            key: state
            for key, state in value.items()
            if isinstance(key, str) and isinstance(state, dict)
        }
        return states

    # Migrate timestamps written by the previous implementation. They mean a
    # Continue was already submitted, so the next matching output must be
    # treated as its response rather than as a brand-new stop.
    attempts = _load_legacy_attempts(path.with_name("retry-attempts.json"))
    return {
        pane_id: {"phase": "awaiting_capacity", "last_prompt_at": timestamp}
        for pane_id, timestamp in attempts.items()
    }


def _load_legacy_attempts(path: Path) -> dict[str, float]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: float(timestamp)
        for key, timestamp in value.items()
        if isinstance(key, str) and isinstance(timestamp, (int, float))
    }


def _save_pane_states(states: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    states_path = path or (state_dir() / PANE_STATES_FILE)
    tmp_path = states_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(states, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, states_path)


def get_pane_state(pane_id: str) -> dict[str, Any]:
    with STATE_LOCK:
        return dict(_load_pane_states(state_dir() / PANE_STATES_FILE).get(pane_id, {}))


def set_pane_state(pane_id: str, state: dict[str, Any]) -> None:
    with STATE_LOCK:
        states = _load_pane_states(state_dir() / PANE_STATES_FILE)
        states[pane_id] = dict(state)
        _save_pane_states(states)


def remove_pane_state(pane_id: str) -> None:
    with STATE_LOCK:
        states = _load_pane_states(state_dir() / PANE_STATES_FILE)
        if pane_id in states:
            states.pop(pane_id, None)
            _save_pane_states(states)


def pane_lock(pane_id: str) -> threading.Lock:
    with PANE_LOCKS_LOCK:
        return PANE_LOCKS.setdefault(pane_id, threading.Lock())


# --------------------------------------------------------------------------
# Error recognition (unchanged from the TUI plugin)


def without_live_composer(text: str) -> str:
    """Exclude the unsent composer while preserving submitted prompt rows."""
    prompts = list(re.finditer(r"(?m)^[›❯] (.*)$", text))
    if prompts:
        last = prompts[-1]
        below = text[last.end():]
        if (last.group(1).strip() != RETRY_TEXT
                and not re.search(r"(?m)^[•■] ", below)
                and not any(error.casefold() in below.casefold() for error in RETRY_ERRORS)):
            text = text[:last.start()]
    return text


def last_submitted_prompt_hash(text: str) -> str | None:
    """Identify a user turn without persisting the prompt's contents."""
    prompts = list(re.finditer(r"(?m)^[›❯] (.*)$", without_live_composer(text)))
    if not prompts:
        return None
    prompt = prompts[-1].group(1).strip()
    if not prompt:
        return None
    return hashlib.sha256(prompt.encode('utf-8')).hexdigest()


def recent_capacity_text(text: str) -> str:
    """Bound the terminal tail by conversational markers, not the last row.

    A subagent completion may follow the error. A newer user prompt supersedes
    it, so an already handled error cannot keep waking the agent indefinitely.
    Terminal history is bounded by RECENT_LINES as well as RECENT_MESSAGES.
    """
    # The final prompt is the live composer (often an italic ghost hint), not
    # a submitted message. It has no Codex output following it. Keep an echoed
    # Continue as a boundary for duplicate suppression, including old states.
    text = without_live_composer(text)
    messages = re.split(r"(?m)(?=^[•›❯] )", text)
    tail = "".join(messages[-RECENT_MESSAGES:])
    prompts = list(re.finditer(r"(?m)^[›❯] ", tail))
    if prompts:
        tail = tail[prompts[-1].start():]
        # A user's quoted error is not an error emitted by Codex.
        tail = tail.partition("\n")[2]
    return tail


def has_recent_capacity(text: str) -> bool:
    # Terminal wrapping can split even the word 'capacity' on a narrow pane.
    compact = lambda value: "".join(value.casefold().split())
    return compact(CAPACITY_ERROR) in compact(recent_capacity_text(text))


def recent_retry_error(text: str) -> str | None:
    compact = lambda value: "".join(value.casefold().split())
    tail = compact(recent_capacity_text(text))
    matches = [(tail.rfind(compact(CAPACITY_ERROR)), CAPACITY_ERROR),
               (tail.rfind(compact('■ ' + COMPACT_STREAM_ERROR)), COMPACT_STREAM_ERROR)]
    position, error = max(matches)
    return error if position >= 0 else None


def recent_turn_error(text: str) -> str | None:
    """Return the visible DSH ``turn error`` divider when it is current.

    DSH renders a failed turn as a divider such as
    ``turn error · The engine is currently overloaded, please try again later``.
    An error followed by new assistant/tool activity belongs to an older turn;
    queue rows, the composer and the footer are chrome, not activity.
    """
    lines = text.splitlines()

    found = None
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:[─━—-]+\s*)?turn\s+error\s*·", line, re.IGNORECASE):
            found = index
    if found is None:
        return None

    # A later transcript row means this error is no longer the latest turn.
    # Our own submitted ``Продолжить`` is not new activity; it only becomes
    # stale once the retry produces a thinking/tool/assistant row (or another
    # turn error, which is selected as the newer divider above).
    activity = re.compile(r"^\s*(?:[⚓•●■✉✓🌓]|[›❯]\s+\S)")
    own_retry = re.compile(r"^[›❯]\s+" + re.escape(RETRY_TEXT) + r"\s*$", re.IGNORECASE)
    for line in lines[found + 1:]:
        if not activity.search(line):
            continue
        if own_retry.match(line.strip()):
            continue
        return None
    return lines[found]


def has_recent_turn_error(text: str) -> bool:
    return recent_turn_error(text) is not None


def capacity_error_follows_continue(read_text: str) -> bool:
    # The same pre-send screen can linger briefly in a mirror. Only an echoed
    # submitted Continue followed by an error starts another cooldown.
    compact = "".join(read_text.casefold().split())
    error = recent_retry_error(read_text)
    return bool(error and compact.rfind("".join(error.casefold().split()))
                > compact.rfind(RETRY_TEXT.casefold()) >= 0)


# --------------------------------------------------------------------------
# Retry state machine (unchanged from the TUI plugin)


def _turn_send_and_track(pane_id: str, state: dict[str, Any], reason: str) -> None:
    pending = dict(state)
    pending["phase"] = "turn_sending"
    pending["agent_kind"] = DSH_TUI_AGENT
    pending.pop("retry_at", None)
    set_pane_state(pane_id, pending)
    try:
        delivery = send_continuation(pane_id, DSH_TUI_AGENT)
    except Exception as exc:
        pending["phase"] = "turn_delivery_failed"
        pending["retry_at"] = time.time() + RETRY_DELAY_SECONDS
        pending["delivery_error"] = str(exc)
        set_pane_state(pane_id, pending)
        report_attention(
            pane_id,
            f"не удалось отправить «{RETRY_TEXT}»: {exc}",
            label="ошибка хода",
            agent_kind=DSH_TUI_AGENT,
        )
        log(f"automatic DSH continuation failed for terminal {pane_id}: {exc}")
        return

    pending["phase"] = "turn_cooldown"
    pending["retry_at"] = time.time() + RETRY_DELAY_SECONDS
    pending["last_prompt_at"] = time.time()
    pending["error_kind"] = "turn_error"
    pending.pop("delivery_error", None)
    set_pane_state(pane_id, pending)
    clear_attention(pane_id, DSH_TUI_AGENT)
    log(f"sent {RETRY_TEXT!r} to DSH TUI terminal {pane_id} via {delivery}; {reason}")


def handle_turn_error(
    pane_id: str,
    matched_line: str,
    read_text: str = "",
) -> None:
    if TURN_ERROR_MARKER not in matched_line.casefold():
        return

    with pane_lock(pane_id):
        agent = agent_for_pane(pane_id)
        if agent is None or agent_kind(agent) != DSH_TUI_AGENT:
            return
        if str(agent.get("agent_status") or "") not in READY_STATUSES:
            return
        if not read_text:
            read_text = read_pane_text(pane_id)
        if not has_recent_turn_error(read_text):
            return
        state = get_pane_state(pane_id)
        phase = state.get("phase")
        if phase in {"turn_cooldown", "turn_sending", "turn_delivery_failed"}:
            return
        if phase == "sending":
            log(f"ignored duplicate turn-error event while sending to terminal {pane_id}")
            return
        _turn_send_and_track(pane_id, state, "initial DSH turn error")


def run_turn_retry(pane_id: str, scheduled_at: float) -> None:
    with pane_lock(pane_id):
        state = get_pane_state(pane_id)
        if state.get("phase") not in {"turn_cooldown", "turn_delivery_failed"}:
            return
        try:
            if float(state.get("retry_at") or 0) != scheduled_at:
                return
        except (TypeError, ValueError):
            return
        if scheduled_at > time.time():
            return

        agent = agent_for_pane(pane_id)
        if agent is None or agent_kind(agent) != DSH_TUI_AGENT:
            remove_pane_state(pane_id)
            clear_attention(pane_id, DSH_TUI_AGENT)
            log(f"no live DSH TUI in terminal {pane_id}; canceled turn-error retry")
            return
        if str(agent.get("agent_status") or "") not in READY_STATUSES:
            # Preserve the deadline; a working or blocked pane is checked again.
            return
        read_text = read_pane_text(pane_id)
        if not has_recent_turn_error(read_text):
            remove_pane_state(pane_id)
            clear_attention(pane_id, DSH_TUI_AGENT)
            log(f"no unresolved turn error in {pane_id}; canceled retry")
            return
        _turn_send_and_track(pane_id, state, "10-minute DSH turn-error retry")


def inspect_idle_pane(pane_id: str) -> None:
    """Reconcile on idle events and periodic scans, including missed output."""
    try:
        if _codex_status_for_pane(pane_id) in READY_STATUSES:
            text = read_pane_text(pane_id)
            error = recent_retry_error(text)
            if error:
                handle_capacity_error(pane_id, error, text)
            else:
                with pane_lock(pane_id):
                    if get_pane_state(pane_id):
                        remove_pane_state(pane_id)
                        clear_attention(pane_id)
            return

        agent = agent_for_pane(pane_id)
        if agent is None or agent_kind(agent) != DSH_TUI_AGENT:
            return
        if str(agent.get("agent_status") or "") not in READY_STATUSES:
            return
        text = read_pane_text(pane_id)
        if has_recent_turn_error(text):
            handle_turn_error(pane_id, TURN_ERROR_MARKER, text)
        else:
            with pane_lock(pane_id):
                if get_pane_state(pane_id):
                    remove_pane_state(pane_id)
                    clear_attention(pane_id, DSH_TUI_AGENT)
    except Exception as exc:
        log(f"could not inspect idle agent terminal {pane_id}: {exc}")


def _send_and_track(pane_id: str, state: dict[str, Any], reason: str, observed_text: str) -> None:
    pending = dict(state)
    prompt_hash = last_submitted_prompt_hash(observed_text)
    if prompt_hash:
        pending['trigger_prompt_hash'] = prompt_hash
    pending["phase"] = "sending"
    pending.pop("retry_at", None)
    set_pane_state(pane_id, pending)
    try:
        delivery = send_continuation(pane_id)
    except Exception as exc:
        pending["phase"] = "delivery_failed"
        pending["delivery_error"] = str(exc)
        set_pane_state(pane_id, pending)
        report_attention(pane_id, f"не удалось отправить «{RETRY_TEXT}»: {exc}")
        log(f"automatic continuation failed for terminal {pane_id}: {exc}")
        return

    pending["phase"] = "awaiting_capacity"
    pending["last_prompt_at"] = time.time()
    pending.pop("delivery_error", None)
    set_pane_state(pane_id, pending)
    clear_attention(pane_id)
    log(f"sent {RETRY_TEXT!r} to Codex terminal {pane_id} via {delivery}; {reason}")


def handle_capacity_error(
    pane_id: str,
    matched_line: str,
    read_text: str = "",
) -> None:
    if not any(error.casefold() in matched_line.casefold() for error in RETRY_ERRORS):
        return

    with pane_lock(pane_id):
        if _codex_status_for_pane(pane_id) not in READY_STATUSES:
            return
        if not read_text:
            read_text = read_pane_text(pane_id)
        current_error = recent_retry_error(read_text)
        if not current_error:
            return
        state = get_pane_state(pane_id)
        phase = state.get("phase")
        previous_prompt = state.get('trigger_prompt_hash')
        current_prompt = last_submitted_prompt_hash(read_text)
        retry_prompt = hashlib.sha256(RETRY_TEXT.encode('utf-8')).hexdigest()
        if (phase in {'awaiting_capacity', 'delivery_failed', 'cooldown'}
                and previous_prompt and current_prompt
                and current_prompt not in {previous_prompt, retry_prompt}):
            # A different submitted user message starts a new turn. An older
            # retry phase cannot suppress that turn's first capacity error.
            _send_and_track(pane_id, {}, 'capacity in a new user turn', read_text)
            return
        if phase == "cooldown":
            return
        if phase in {"awaiting_capacity", "delivery_failed"}:
            if not capacity_error_follows_continue(read_text):
                # Some events carry the matched line but no surrounding
                # terminal text. Read the current screen once so a real
                # response after Continue is not mistaken for stale text.
                try:
                    read_text = read_pane_text(pane_id)
                except Exception as exc:
                    log(f"could not verify capacity response in terminal {pane_id}: {exc}")
                if not capacity_error_follows_continue(read_text):
                    return
            retry_at = time.time() + RETRY_DELAY_SECONDS
            state["phase"] = "cooldown"
            state["retry_at"] = retry_at
            state["error_kind"] = 'compact_stream' if current_error == COMPACT_STREAM_ERROR else 'capacity'
            state.pop("delivery_error", None)
            set_pane_state(pane_id, state)
            report_attention(
                pane_id,
                "ошибка снова возникла после «Продолжить»; повтор через 10 минут",
                ttl_ms=RETRY_DELAY_SECONDS * 1000,
                label='ошибка потока' if current_error == COMPACT_STREAM_ERROR else 'модель перегружена',
            )
            log(f"capacity error followed Continue in terminal {pane_id}; retry scheduled")
            return
        if phase == "sending":
            log(f"ignored duplicate capacity event while sending to terminal {pane_id}")
            return

        _send_and_track(pane_id, state, "initial capacity response", read_text)


def _codex_status_for_pane(pane_id: str) -> str | None:
    for agent in agents_snapshot():
        if str(agent.get("pane_id") or "") == pane_id and agent_is_codex(agent):
            return str(agent.get("agent_status") or "")
    return None


def run_scheduled_retry(pane_id: str, scheduled_at: float) -> None:
    with pane_lock(pane_id):
        state = get_pane_state(pane_id)
        if state.get("phase") != "cooldown":
            return
        if float(state.get("retry_at") or 0) != scheduled_at:
            return
        if scheduled_at > time.time():
            return
        try:
            agent_status = _codex_status_for_pane(pane_id)
        except Exception as exc:
            agent_status = None
            reason = f"could not verify Codex terminal before retry: {exc}"
        else:
            reason = "Herdr no longer recognizes Codex in this terminal"
        if agent_status is None:
            state["phase"] = "delivery_failed"
            state.pop("retry_at", None)
            state["delivery_error"] = reason
            set_pane_state(pane_id, state)
            report_attention(pane_id, reason)
            log(f"scheduled continuation skipped for terminal {pane_id}: {reason}")
            return
        if agent_status not in READY_STATUSES:
            # Preserve the original deadline; a working/blocked/unknown pane
            # can be checked again once it is ready, without typing into it.
            return
        read_text = read_pane_text(pane_id)
        if not recent_retry_error(read_text):
            remove_pane_state(pane_id)
            clear_attention(pane_id)
            log(f"no unresolved recent capacity error in {pane_id}; canceled retry")
            return
        _send_and_track(pane_id, state, "10-minute capacity retry", read_text)


def retry_scheduler() -> None:
    next_scan = 0.0
    while not STOP.is_set():
        now = time.time()
        with STATE_LOCK:
            states = _load_pane_states(state_dir() / PANE_STATES_FILE)
        for pane_id, state in states.items():
            phase = state.get("phase")
            if phase not in {"cooldown", "turn_cooldown", "turn_delivery_failed"}:
                continue
            try:
                retry_at = float(state.get("retry_at"))
            except (TypeError, ValueError):
                continue
            if retry_at <= now:
                try:
                    if phase == "cooldown":
                        run_scheduled_retry(pane_id, retry_at)
                    else:
                        run_turn_retry(pane_id, retry_at)
                except Exception as exc:
                    log(f"could not check scheduled retry for {pane_id}: {exc}")
        if time.monotonic() >= next_scan:
            try:
                for agent in agents_snapshot():
                    if agent_is_watched(agent) and agent.get("agent_status") in READY_STATUSES:
                        inspect_idle_pane(str(agent.get("pane_id") or ""))
            except Exception as exc:
                log(f"could not scan idle agent terminals: {exc}")
            next_scan = time.monotonic() + SCAN_INTERVAL_SECONDS
        if STOP.wait(1):
            return


def start_retry_scheduler() -> None:
    threading.Thread(
        target=retry_scheduler,
        name="capacity-retry-scheduler",
        daemon=True,
    ).start()


def process_agent_status(pane_id: str, status: str) -> None:
    if status in READY_STATUSES:
        inspect_idle_pane(pane_id)


# --------------------------------------------------------------------------
# Event stream: hc events --json (snapshot line, then JSON-RPC notifications)


def event_stream():
    """Yield (kind, address, data) from ``hc events --json``.

    The first line is a full snapshot; later lines are
    ``{jsonrpc, method: "event", params: {type, terminal, seq, data}}``.
    """
    global CURRENT_EVENTS_PROCESS
    process = subprocess.Popen(
        [herdr_app.hc_binary(), "events", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=herdr_app.NO_WINDOW,
    )
    CURRENT_EVENTS_PROCESS = process
    try:
        first = process.stdout.readline()
        if not first:
            raise RuntimeError("hc events closed the stream")
        snapshot = json.loads(first)
        yield "snapshot", "", snapshot
        for raw in process.stdout:
            raw = raw.strip()
            if not raw:
                continue
            message = json.loads(raw)
            params = message.get("params", {})
            yield str(params.get("type") or ""), str(params.get("terminal") or ""), params.get("data") or {}
    finally:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        CURRENT_EVENTS_PROCESS = None


def reconcile_watched_panes() -> None:
    """Register every watched agent terminal known to the core."""
    for agent in agents_snapshot():
        if agent_is_watched(agent):
            start_pane_watcher(str(agent.get("pane_id") or ""))


def process_lifecycle_event(kind: str, address: str, data: dict[str, Any]) -> None:
    if kind == "snapshot":
        for terminal in data.get("terminals", []):
            status = terminal.get("status")
            if isinstance(status, dict) and agent_is_watched({"agent": status.get("app")}):
                start_pane_watcher(str(terminal.get("address") or ""))
        return
    if kind == "created":
        status = data.get("status")
        if isinstance(status, dict) and agent_is_watched({"agent": status.get("app")}):
            start_pane_watcher(address)
        return
    if kind == "status":
        status = data.get("status")
        if isinstance(status, dict) and agent_is_watched({"agent": status.get("app")}):
            start_pane_watcher(address)
            process_agent_status(address, str(status.get("state") or ""))
        return
    if kind == "finished":
        remove_pane_state(address)
        clear_attention(address)
        with WATCHING_LOCK:
            WATCHING.discard(address)
        return


def monitor() -> int:
    directory = state_dir()
    herdr_app.clear_stop(directory)
    lock = herdr_app.FileLock(directory / "monitor.lock")
    deadline = time.monotonic() + 20
    while not lock.acquire():
        if time.monotonic() >= deadline:
            log("another watcher still owns the monitor lock; exiting")
            return 0
        time.sleep(0.5)

    pid_path = directory / "monitor.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")

    def _term(signum, frame):
        STOP.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _term)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _term)

    start_retry_scheduler()

    def _stop_watchdog() -> None:
        # Cross-platform stop: the stop file is checked even while the event
        # stream is idle; terminating hc events unblocks the readline.
        directory = state_dir()
        while not STOP.is_set():
            if herdr_app.stop_requested(directory):
                STOP.set()
                process = CURRENT_EVENTS_PROCESS
                if process is not None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                return
            if STOP.wait(1):
                return

    threading.Thread(target=_stop_watchdog, name="stop-watchdog", daemon=True).start()
    retry_delay = 1
    try:
        while not STOP.is_set():
            connected_at: float | None = None
            try:
                stream = event_stream()
                connected_at = time.monotonic()
                # Subscribe first, then reconcile the snapshot so a watched
                # terminal created during startup cannot fall between the two.
                reconcile_watched_panes()
                for kind, address, data in stream:
                    if STOP.is_set() or herdr_app.stop_requested(directory):
                        break
                    try:
                        process_lifecycle_event(kind, address, data)
                    except Exception as exc:
                        log(f"could not process {kind} event for {address}: {exc}")
                if not STOP.is_set():
                    log("hc event stream closed; reconnecting")
            except Exception as exc:
                log(f"hc event stream disconnected: {exc}; reconnecting")

            if STOP.is_set() or herdr_app.stop_requested(directory):
                break
            if connected_at is not None and time.monotonic() - connected_at >= 60:
                retry_delay = 1
            if STOP.wait(retry_delay):
                break
            retry_delay = min(retry_delay * 2, 30)
        return 0
    finally:
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
        lock.release()


def start() -> int:
    directory = state_dir()
    herdr_app.clear_stop(directory)
    lock_probe = herdr_app.FileLock(directory / "monitor.lock")
    if not lock_probe.acquire():
        print("Agent guard watcher is already running.")
        return 0
    lock_probe.release()
    log_path = directory / "monitor.log"
    command = [sys.executable, str(Path(__file__).resolve()), "monitor"]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log_path.open("ab") as output:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **kwargs,
        )
    print(f"Started Agent guard watcher (pid {process.pid}).")
    return 0


def _watcher_running(directory: Path) -> int | None:
    """Return the watcher pid when the monitor lock is held, else None."""
    probe = herdr_app.FileLock(directory / "monitor.lock")
    if probe.acquire():
        probe.release()
        return None
    try:
        return int((directory / "monitor.pid").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def stop() -> int:
    directory = state_dir()
    pid = _watcher_running(directory)
    if pid is None:
        print("Agent guard watcher is not running.")
        return 0
    herdr_app.request_stop(directory)
    if pid and os.name != "nt" and herdr_app.pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(f"Stopped Agent guard watcher (pid {pid}).")
    return 0


def status() -> int:
    directory = state_dir()
    pid = _watcher_running(directory)
    if pid is None:
        print("Agent guard watcher is not running.")
    elif pid:
        print(f"Agent guard watcher is running (pid {pid}).")
    else:
        print("Agent guard watcher is running.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"start", "monitor", "stop", "status"}:
        print("usage: guard.py {start|monitor|stop|status}", file=sys.stderr)
        return 2
    command = argv[1]
    if command == "start":
        return start()
    if command == "monitor":
        return monitor()
    if command == "stop":
        return stop()
    return status()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
