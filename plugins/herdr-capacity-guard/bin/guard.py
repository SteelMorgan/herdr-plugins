#!/usr/bin/env python3
"""Watch Codex and DSH TUI panes and retry retryable turn failures."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO


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
PLUGIN_SOURCE = "user:herdr-capacity-guard"
PANE_STATES_FILE = "pane-states.json"
RECENT_MESSAGES = 8
RECENT_LINES = 240
SCAN_INTERVAL_SECONDS = 5
READY_STATUSES = {"idle", "done"}

STOP = threading.Event()
WATCHING: set[str] = set()
STOPPED_PANES: set[str] = set()
WATCHER_SOCKETS: dict[str, socket.socket] = {}
WATCHING_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
PANE_LOCKS: dict[str, threading.Lock] = {}
PANE_LOCKS_LOCK = threading.Lock()


def state_dir() -> Path:
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not value:
        raise RuntimeError("HERDR_PLUGIN_STATE_DIR is not set")
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def socket_path() -> str:
    value = os.environ.get("HERDR_SOCKET_PATH")
    if not value:
        raise RuntimeError("HERDR_SOCKET_PATH is not set")
    return value


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def read_line(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            if data:
                return bytes(data)
            return b""
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            return bytes(data[:newline])


def api_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    request = {"id": f"codex-capacity:{method}", "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(8)
        sock.connect(socket_path())
        sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        raw = read_line(sock)
    if not raw:
        raise RuntimeError(f"Herdr closed the connection for {method}")
    response = json.loads(raw.decode("utf-8"))
    if "error" in response:
        error = response["error"]
        raise RuntimeError(f"{error.get('code', 'api_error')}: {error.get('message', '')}")
    return response


def subscribe(subscriptions: list[dict[str, Any]]) -> tuple[socket.socket, BinaryIO]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path())
    except Exception:
        sock.close()
        raise
    request = {
        "id": "codex-capacity:events",
        "method": "events.subscribe",
        "params": {"subscriptions": subscriptions},
    }
    sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
    stream = sock.makefile("rb")
    first = stream.readline()
    if not first:
        stream.close()
        sock.close()
        raise RuntimeError("Herdr closed the event subscription")
    response = json.loads(first.decode("utf-8"))
    if "error" in response:
        stream.close()
        sock.close()
        error = response["error"]
        raise RuntimeError(f"{error.get('code', 'api_error')}: {error.get('message', '')}")
    return sock, stream


def agents_snapshot() -> list[dict[str, Any]]:
    response = api_request("agent.list", {})
    return response.get("result", {}).get("agents", [])


def agent_kind(agent: dict[str, Any]) -> str:
    return str(agent.get("agent") or "").casefold()


def agent_is_codex(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) == "codex"


def agent_is_dsh_tui(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) == DSH_TUI_AGENT


def agent_is_watched(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) in WATCHED_AGENTS


def agent_for_pane(pane_id: str) -> dict[str, Any] | None:
    """Return the watched agent occupying this pane, if there is exactly one."""
    matches = [
        agent
        for agent in agents_snapshot()
        if str(agent.get("pane_id") or "") == pane_id and agent_is_watched(agent)
    ]
    return matches[0] if len(matches) == 1 else None


def kind_label(kind: str) -> str:
    return "DSH TUI" if kind == DSH_TUI_AGENT else "Codex"


def start_pane_watcher(pane_id: str) -> None:
    if not pane_id:
        return
    with WATCHING_LOCK:
        STOPPED_PANES.discard(pane_id)
        if pane_id in WATCHING:
            return
        WATCHING.add(pane_id)
    thread = threading.Thread(
        target=watch_pane,
        args=(pane_id,),
        name=f"agent-guard-{pane_id}",
        daemon=True,
    )
    thread.start()
    log(f"watching agent pane {pane_id}")


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
            states.pop(pane_id)
            _save_pane_states(states)


def pane_lock(pane_id: str) -> threading.Lock:
    with PANE_LOCKS_LOCK:
        return PANE_LOCKS.setdefault(pane_id, threading.Lock())


def report_attention(
    pane_id: str,
    reason: str,
    ttl_ms: int | None = None,
    notify: bool = True,
    label: str = "модель перегружена",
    agent_kind: str = "codex",
) -> None:
    params: dict[str, Any] = {
        "pane_id": pane_id,
        "source": PLUGIN_SOURCE,
        "agent": agent_kind,
        "state_labels": {
            "working": label,
            "blocked": label,
        },
    }
    if ttl_ms is not None:
        params["ttl_ms"] = max(1, ttl_ms)
    try:
        api_request("pane.report_metadata", params)
    except Exception as exc:  # Keep the notification attempt independent.
        log(f"could not label pane {pane_id}: {exc}")

    if not notify:
        return
    try:
        api_request(
            "notification.show",
            {
                "title": f"{kind_label(agent_kind)} требует внимания",
                "body": f"{pane_id}: {reason}",
                "sound": "none",
            },
        )
    except Exception as exc:
        log(f"could not notify for pane {pane_id}: {exc}")


def clear_attention(pane_id: str, agent_kind: str = "codex") -> None:
    try:
        api_request(
            "pane.report_metadata",
            {
                "pane_id": pane_id,
                "source": PLUGIN_SOURCE,
                "agent": agent_kind,
                "clear_state_labels": True,
            },
        )
    except Exception as exc:
        log(f"could not clear attention label for pane {pane_id}: {exc}")


def docker_mirror_route(pane_id: str) -> tuple[str, str, list[str], str] | None:
    """Read the remote destination from this pane's actual mirror process."""
    info = api_request('pane.process_info', {'pane_id': pane_id})
    processes = info.get('result', {}).get('process_info', {}).get('foreground_processes', [])
    for process in processes:
        argv = process.get('argv') or []
        if len(argv) < 4 or os.path.basename(argv[0]) != 'herdr-mirror' or argv[1] != 'pane':
            continue
        if '--container' not in argv:
            return None
        def option(name: str, default: str | None = None) -> str | None:
            try:
                return argv[argv.index(name) + 1]
            except (ValueError, IndexError):
                return default
        container = option('--container')
        remote_bin = option('--remote-bin')
        docker_bin = option('--docker-bin', 'docker')
        remote_pane = argv[3]
        if (not container or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container)
                or not re.fullmatch(r'w[A-Za-z0-9]+:p[A-Za-z0-9]+', remote_pane)
                or not remote_bin or not docker_bin):
            raise RuntimeError('invalid Docker mirror destination')
        command = shlex.split(remote_bin)
        if not command:
            raise RuntimeError('empty Docker mirror remote command')
        return docker_bin, container, command, remote_pane
    return None


def send_docker_mirror(
    route: tuple[str, str, list[str], str],
    expected_kind: str = "codex",
) -> None:
    docker_bin, container, remote_bin, remote_pane = route
    prefix = [docker_bin, 'exec', container, *remote_bin]

    def remote(*args: str) -> dict[str, Any]:
        result = subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=12, check=False)
        if result.returncode != 0:
            raise RuntimeError('remote Herdr command failed: ' + (result.stderr or result.stdout).strip()[:300])
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError('remote Herdr returned invalid JSON') from exc
        if response.get('error'):
            raise RuntimeError('remote Herdr rejected command: ' + str(response['error'])[:300])
        return response.get('result', {})

    agents = remote('agent', 'list').get('agents', [])
    match = [a for a in agents if a.get('pane_id') == remote_pane and agent_kind(a) == expected_kind]
    if len(match) != 1 or match[0].get('agent_status') not in READY_STATUSES:
        raise RuntimeError(f'remote pane is not a ready {kind_label(expected_kind)} agent')
    result = remote('agent', 'prompt', remote_pane, RETRY_TEXT)
    if result.get('type') != 'agent_prompted':
        raise RuntimeError('remote Herdr did not confirm agent prompt')


def send_continuation(pane_id: str, expected_kind: str = "codex") -> str:
    """Submit the retry through agent control, falling back for mirror panes."""
    try:
        api_request("agent.prompt", {"target": pane_id, "text": RETRY_TEXT})
        return "agent API"
    except Exception as exc:
        if "agent_not_ready:" not in str(exc):
            raise

        agents = agents_snapshot()
        if not any(
            str(agent.get("pane_id") or "") == pane_id and agent_kind(agent) == expected_kind
            for agent in agents
        ):
            raise RuntimeError(
                f"agent.prompt rejected {pane_id}, and Herdr no longer recognizes "
                f"{kind_label(expected_kind)} there"
            ) from exc

        route = docker_mirror_route(pane_id)
        if route is not None:
            # Do not fall back to terminal input on failure: a timeout can mean
            # the remote request was delivered even though its reply was lost.
            send_docker_mirror(route, expected_kind)
            return 'remote Docker agent API'

        herdr = shutil.which("herdr")
        if not herdr:
            raise RuntimeError("Herdr CLI is unavailable for mirror terminal fallback") from exc

        text_result = subprocess.run(
            [herdr, "pane", "send-text", pane_id, RETRY_TEXT],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if text_result.returncode != 0:
            detail = (text_result.stderr or text_result.stdout).strip()
            raise RuntimeError(f"mirror pane rejected retry text: {detail}") from exc

        enter_result = subprocess.run(
            [herdr, "pane", "send-keys", pane_id, "enter"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if enter_result.returncode != 0:
            detail = (enter_result.stderr or enter_result.stdout).strip()
            raise RuntimeError(f"mirror pane rejected Enter: {detail}") from exc
        return "mirror terminal fallback"


def read_pane_text(pane_id: str) -> str:
    response = api_request(
        "pane.read",
        # ANSI recent reads do not drive the agent's scroll interface. Read the
        # live tail even when the operator has scrolled the viewport upward.
        {"pane_id": pane_id, "source": "recent", "format": "ansi", "lines": RECENT_LINES},
    )
    result = response.get("result", {})
    read = result.get("read", {})
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(read.get("text") or "")).replace("\r", "")


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
    compact = lambda value: "".join(value.casefold().split())

    found = None
    for index, line in enumerate(lines):
        if "turnerror" in compact(line):
            found = index
    if found is None:
        return None

    detail = compact(TURN_ERROR_DETAIL)
    window = "".join(compact(line) for line in lines[found:found + 4])
    if detail not in window and not TURN_ERROR_API_ERROR.search(window):
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
        log(f"automatic DSH continuation failed for pane {pane_id}: {exc}")
        return

    pending["phase"] = "turn_cooldown"
    pending["retry_at"] = time.time() + RETRY_DELAY_SECONDS
    pending["last_prompt_at"] = time.time()
    pending["error_kind"] = "turn_error"
    pending.pop("delivery_error", None)
    set_pane_state(pane_id, pending)
    clear_attention(pane_id, DSH_TUI_AGENT)
    log(f"sent {RETRY_TEXT!r} to DSH TUI pane {pane_id} via {delivery}; {reason}")


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
            log(f"ignored duplicate turn-error event while sending to pane {pane_id}")
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
            log(f"no live DSH TUI in pane {pane_id}; canceled turn-error retry")
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
        log(f"could not inspect idle agent pane {pane_id}: {exc}")


def capacity_error_follows_continue(read_text: str) -> bool:
    # The same pre-send screen can linger briefly in a mirror. Only an echoed
    # submitted Continue followed by an error starts another cooldown.
    compact = "".join(read_text.casefold().split())
    error = recent_retry_error(read_text)
    return bool(error and compact.rfind("".join(error.casefold().split()))
                > compact.rfind(RETRY_TEXT.casefold()) >= 0)


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
        log(f"automatic continuation failed for pane {pane_id}: {exc}")
        return

    pending["phase"] = "awaiting_capacity"
    pending["last_prompt_at"] = time.time()
    pending.pop("delivery_error", None)
    set_pane_state(pane_id, pending)
    clear_attention(pane_id)
    log(f"sent {RETRY_TEXT!r} to Codex pane {pane_id} via {delivery}; {reason}")


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
                # Some output_matched events carry the matched line but no
                # surrounding terminal text. Read the current pane once so a
                # real response after Continue is not mistaken for stale text.
                try:
                    read_text = read_pane_text(pane_id)
                except Exception as exc:
                    log(f"could not verify capacity response in pane {pane_id}: {exc}")
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
            log(f"capacity error followed Continue in pane {pane_id}; retry scheduled")
            return
        if phase == "sending":
            log(f"ignored duplicate capacity event while sending to pane {pane_id}")
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
            reason = f"could not verify Codex pane before retry: {exc}"
        else:
            reason = "Herdr no longer recognizes Codex in this pane"
        if agent_status is None:
            state["phase"] = "delivery_failed"
            state.pop("retry_at", None)
            state["delivery_error"] = reason
            set_pane_state(pane_id, state)
            report_attention(pane_id, reason)
            log(f"scheduled continuation skipped for pane {pane_id}: {reason}")
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
                log(f"could not scan idle agent panes: {exc}")
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


def watch_pane(pane_id: str) -> None:
    retry_delay = 1
    try:
        while not STOP.is_set():
            with WATCHING_LOCK:
                if pane_id in STOPPED_PANES:
                    break

            sock: socket.socket | None = None
            stream: BinaryIO | None = None
            try:
                sock, stream = subscribe(
                    [
                        {
                            "type": "pane.output_matched",
                            "pane_id": pane_id,
                            "source": "visible",
                            "lines": 80,
                            "match": {"type": "substring", "value": CAPACITY_ERROR},
                            "strip_ansi": True,
                        },
                        {
                            "type": "pane.output_matched",
                            "pane_id": pane_id,
                            "source": "visible",
                            "lines": 80,
                            "match": {"type": "substring", "value": COMPACT_STREAM_ERROR},
                            "strip_ansi": True,
                        },
                        {
                            "type": "pane.output_matched",
                            "pane_id": pane_id,
                            "source": "visible",
                            "lines": 80,
                            "match": {"type": "substring", "value": TURN_ERROR_MARKER},
                            "strip_ansi": True,
                        },
                        {
                            "type": "pane.agent_status_changed",
                            "pane_id": pane_id,
                        },
                    ]
                )
                with WATCHING_LOCK:
                    if pane_id in STOPPED_PANES:
                        break
                    WATCHER_SOCKETS[pane_id] = sock
                state = get_pane_state(pane_id)
                if state.get("phase") in {"cooldown", "turn_cooldown", "turn_delivery_failed"}:
                    try:
                        remaining_ms = int(
                            max(1, float(state.get("retry_at") or time.time()) - time.time())
                            * 1000
                        )
                        if state.get("phase") == "cooldown":
                            report_attention(
                                pane_id,
                                "ошибка снова возникла после «Продолжить»; повтор через 10 минут",
                                ttl_ms=remaining_ms,
                                notify=False,
                                label='ошибка потока' if state.get('error_kind') == 'compact_stream' else 'модель перегружена',
                            )
                        else:
                            report_attention(
                                pane_id,
                                "turn error после «Продолжить»; повтор через 10 минут",
                                ttl_ms=remaining_ms,
                                notify=False,
                                label="ошибка хода",
                                agent_kind=DSH_TUI_AGENT,
                            )
                    except (TypeError, ValueError):
                        pass
                try:
                    initial_text = read_pane_text(pane_id)
                    error = recent_retry_error(initial_text)
                    if error:
                        handle_capacity_error(pane_id, error, initial_text)
                    turn_error = recent_turn_error(initial_text)
                    if turn_error is not None:
                        handle_turn_error(pane_id, TURN_ERROR_MARKER, initial_text)
                except Exception as exc:
                    log(f"could not inspect current output for pane {pane_id}: {exc}")
                while not STOP.is_set():
                    raw = stream.readline()
                    if not raw:
                        log(f"pane watcher {pane_id} event stream closed; reconnecting")
                        break
                    event = json.loads(raw.decode("utf-8"))
                    if "error" in event:
                        error = event["error"]
                        log(
                            f"pane watcher {pane_id} received {error.get('code', 'api_error')}; reconnecting"
                        )
                        break
                    data = event.get("data", {})
                    kind = event.get("event")
                    if kind in {"pane.output_matched", "pane_output_matched"}:
                        read = data.get("read", {})
                        matched_line = str(data.get("matched_line", ""))
                        read_text = str(read.get("text") or "")
                        if TURN_ERROR_MARKER in matched_line.casefold():
                            handle_turn_error(pane_id, matched_line, read_text)
                        else:
                            handle_capacity_error(pane_id, matched_line, read_text)
                    elif kind in {
                        "pane.agent_status_changed",
                        "pane_agent_status_changed",
                    }:
                        process_agent_status(pane_id, str(data.get("agent_status") or ""))
                retry_delay = 1
            except Exception as exc:
                log(f"pane watcher {pane_id} disconnected: {exc}; reconnecting")
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                with WATCHING_LOCK:
                    if WATCHER_SOCKETS.get(pane_id) is sock:
                        WATCHER_SOCKETS.pop(pane_id, None)

            with WATCHING_LOCK:
                if pane_id in STOPPED_PANES:
                    break
            if STOP.wait(retry_delay):
                break
            retry_delay = min(retry_delay * 2, 30)
    finally:
        with WATCHING_LOCK:
            sock = WATCHER_SOCKETS.pop(pane_id, None)
            WATCHING.discard(pane_id)


def process_lifecycle_event(event: dict[str, Any]) -> None:
    kind = event.get("event")
    data = event.get("data", {})
    if (
        kind in {"pane.agent_detected", "pane_agent_detected"}
        and agent_is_watched({"agent": data.get("agent")})
    ):
        start_pane_watcher(str(data.get("pane_id") or ""))
    elif kind in {"pane.closed", "pane_closed", "pane.exited", "pane_exited"}:
        pane_id = str(data.get("pane_id") or "")
        remove_pane_state(pane_id)
        with WATCHING_LOCK:
            STOPPED_PANES.add(pane_id)
            sock = WATCHER_SOCKETS.get(pane_id)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def reconcile_watched_panes() -> None:
    """Start output watchers for every watched agent pane known to Herdr."""
    for agent in agents_snapshot():
        if agent_is_watched(agent):
            start_pane_watcher(str(agent.get("pane_id") or ""))


def monitor() -> int:
    lock_path = state_dir() / "monitor.lock"
    lock_file = lock_path.open("a+")
    deadline = time.monotonic() + 20
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                log("another watcher still owns the monitor lock; exiting")
                return 0
            time.sleep(0.5)

    pid_path = state_dir() / "monitor.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
    start_retry_scheduler()
    retry_delay = 1
    try:
        while not STOP.is_set():
            sock: socket.socket | None = None
            stream: BinaryIO | None = None
            connected_at: float | None = None
            try:
                sock, stream = subscribe(
                    [
                        {"type": "pane.agent_detected"},
                        {"type": "pane.closed"},
                        {"type": "pane.exited"},
                    ]
                )
                connected_at = time.monotonic()
                # Subscribe first, then reconcile the snapshot so a watched pane
                # created during startup cannot fall between the two operations.
                reconcile_watched_panes()
                while not STOP.is_set():
                    raw = stream.readline()
                    if not raw:
                        log("Herdr event stream closed; reconnecting")
                        break
                    event = json.loads(raw.decode("utf-8"))
                    if "error" in event:
                        code = event["error"].get("code", "api_error")
                        if code == "events_lost":
                            log("event history was lost; refreshing watched pane list")
                            reconcile_watched_panes()
                        else:
                            log(f"Herdr event stream failed: {code}; reconnecting")
                        break
                    process_lifecycle_event(event)
            except Exception as exc:
                log(f"Herdr event stream disconnected: {exc}; reconnecting")
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if STOP.is_set():
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
        lock_file.close()


def start() -> int:
    directory = state_dir()
    log_path = directory / "monitor.log"
    command = [sys.executable, str(Path(__file__).resolve()), "monitor"]
    with log_path.open("ab") as output:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    print(f"Started Agent guard watcher (pid {process.pid}).")
    return 0


def _pid_is_guard(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return b"guard.py monitor" in cmdline


def stop() -> int:
    pid_path = state_dir() / "monitor.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        print("Agent guard watcher is not running.")
        return 0
    if not _pid_is_guard(pid):
        print("No active watcher matches the saved process id.")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"Stopped Agent guard watcher (pid {pid}).")
    return 0


def status() -> int:
    pid_path = state_dir() / "monitor.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        print("Agent guard watcher is not running.")
        return 0
    if _pid_is_guard(pid):
        print(f"Agent guard watcher is running (pid {pid}).")
    else:
        print("Agent guard watcher is not running (stale pid file).")
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
