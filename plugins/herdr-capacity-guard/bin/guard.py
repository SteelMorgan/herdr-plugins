#!/usr/bin/env python3
"""Watch Codex panes and retry model-capacity errors after a bounded wait."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO


CAPACITY_ERROR = "Selected model is at capacity. Please try a different model."
RETRY_TEXT = "Продолжить"
RETRY_DELAY_SECONDS = 10 * 60
PLUGIN_SOURCE = "user:herdr-capacity-guard"
PANE_STATES_FILE = "pane-states.json"

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


def agent_is_codex(agent: dict[str, Any]) -> bool:
    return str(agent.get("agent") or "").casefold() == "codex"


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
        name=f"capacity-{pane_id}",
        daemon=True,
    )
    thread.start()
    log(f"watching Codex pane {pane_id}")


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
) -> None:
    params: dict[str, Any] = {
        "pane_id": pane_id,
        "source": PLUGIN_SOURCE,
        "agent": "codex",
        "state_labels": {
            "working": "модель перегружена",
            "blocked": "модель перегружена",
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
                "title": "Codex требует внимания",
                "body": f"{pane_id}: {reason}",
                "sound": "none",
            },
        )
    except Exception as exc:
        log(f"could not notify for pane {pane_id}: {exc}")


def clear_attention(pane_id: str) -> None:
    try:
        api_request(
            "pane.report_metadata",
            {
                "pane_id": pane_id,
                "source": PLUGIN_SOURCE,
                "agent": "codex",
                "clear_state_labels": True,
            },
        )
    except Exception as exc:
        log(f"could not clear attention label for pane {pane_id}: {exc}")


def send_continuation(pane_id: str) -> str:
    """Submit the retry through agent control, falling back for mirror panes."""
    try:
        api_request("agent.prompt", {"target": pane_id, "text": RETRY_TEXT})
        return "agent API"
    except Exception as exc:
        if "agent_not_ready:" not in str(exc):
            raise

        agents = agents_snapshot()
        if not any(
            str(agent.get("pane_id") or "") == pane_id and agent_is_codex(agent)
            for agent in agents
        ):
            raise RuntimeError(
                f"agent.prompt rejected {pane_id}, and Herdr no longer recognizes Codex there"
            ) from exc

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
        {"pane_id": pane_id, "source": "visible", "format": "text", "lines": 80},
    )
    result = response.get("result", {})
    read = result.get("read", {})
    return str(read.get("text") or "")


def capacity_error_follows_continue(read_text: str) -> bool:
    text = read_text.casefold()
    error_at = text.rfind(CAPACITY_ERROR.casefold())
    continue_at = text.rfind(RETRY_TEXT.casefold())
    return continue_at >= 0 and error_at > continue_at


def _send_and_track(pane_id: str, state: dict[str, Any], reason: str) -> None:
    pending = dict(state)
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
    if CAPACITY_ERROR.casefold() not in matched_line.casefold():
        return

    with pane_lock(pane_id):
        state = get_pane_state(pane_id)
        phase = state.get("phase")
        if phase == "cooldown":
            log(f"capacity error remains during scheduled wait for pane {pane_id}")
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
                    log(f"ignored capacity text before last Continue in pane {pane_id}")
                    return
            retry_at = time.time() + RETRY_DELAY_SECONDS
            state["phase"] = "cooldown"
            state["retry_at"] = retry_at
            state.pop("delivery_error", None)
            set_pane_state(pane_id, state)
            report_attention(
                pane_id,
                "модель снова перегружена после «Продолжить»; повтор через 10 минут",
                ttl_ms=RETRY_DELAY_SECONDS * 1000,
            )
            log(f"capacity error followed Continue in pane {pane_id}; retry scheduled")
            return
        if phase == "sending":
            log(f"ignored duplicate capacity event while sending to pane {pane_id}")
            return

        _send_and_track(pane_id, state, "initial capacity response")


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
        if agent_status is not None and agent_status.casefold() == "done":
            remove_pane_state(pane_id)
            clear_attention(pane_id)
            log(f"Codex completed in pane {pane_id}; canceled scheduled retry")
            return
        if agent_status is None:
            state["phase"] = "delivery_failed"
            state.pop("retry_at", None)
            state["delivery_error"] = reason
            set_pane_state(pane_id, state)
            report_attention(pane_id, reason)
            log(f"scheduled continuation skipped for pane {pane_id}: {reason}")
            return
        _send_and_track(pane_id, state, "10-minute capacity retry")


def retry_scheduler() -> None:
    while not STOP.is_set():
        now = time.time()
        with STATE_LOCK:
            states = _load_pane_states(state_dir() / PANE_STATES_FILE)
        for pane_id, state in states.items():
            if state.get("phase") != "cooldown":
                continue
            try:
                retry_at = float(state.get("retry_at"))
            except (TypeError, ValueError):
                continue
            if retry_at <= now:
                run_scheduled_retry(pane_id, retry_at)
        if STOP.wait(1):
            return


def start_retry_scheduler() -> None:
    threading.Thread(
        target=retry_scheduler,
        name="capacity-retry-scheduler",
        daemon=True,
    ).start()


def process_agent_status(pane_id: str, status: str) -> None:
    if status != "done":
        return
    with pane_lock(pane_id):
        state = get_pane_state(pane_id)
        if state.get("phase") in {"awaiting_capacity", "cooldown", "delivery_failed"}:
            remove_pane_state(pane_id)
            clear_attention(pane_id)
            log(f"Codex completed in pane {pane_id}; cleared retry state")


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
                if state.get("phase") == "cooldown":
                    try:
                        remaining_ms = int(
                            max(1, float(state.get("retry_at") or time.time()) - time.time())
                            * 1000
                        )
                        report_attention(
                            pane_id,
                            "модель снова перегружена после «Продолжить»; повтор через 10 минут",
                            ttl_ms=remaining_ms,
                            notify=False,
                        )
                    except (TypeError, ValueError):
                        pass
                try:
                    initial_text = read_pane_text(pane_id)
                    if CAPACITY_ERROR.casefold() in initial_text.casefold():
                        handle_capacity_error(pane_id, CAPACITY_ERROR, initial_text)
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
                        handle_capacity_error(
                            pane_id,
                            str(data.get("matched_line", "")),
                            str(read.get("text") or ""),
                        )
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
        and str(data.get("agent") or "").casefold() == "codex"
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


def reconcile_codex_panes() -> None:
    """Start output watchers for every Codex pane known to Herdr right now."""
    for agent in agents_snapshot():
        if agent_is_codex(agent):
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
                # Subscribe first, then reconcile the snapshot so a Codex pane
                # created during startup cannot fall between the two operations.
                reconcile_codex_panes()
                while not STOP.is_set():
                    raw = stream.readline()
                    if not raw:
                        log("Herdr event stream closed; reconnecting")
                        break
                    event = json.loads(raw.decode("utf-8"))
                    if "error" in event:
                        code = event["error"].get("code", "api_error")
                        if code == "events_lost":
                            log("event history was lost; refreshing Codex pane list")
                            reconcile_codex_panes()
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
    print(f"Started Codex capacity watcher (pid {process.pid}).")
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
        print("Codex capacity watcher is not running.")
        return 0
    if not _pid_is_guard(pid):
        print("No active watcher matches the saved process id.")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"Stopped Codex capacity watcher (pid {pid}).")
    return 0


def status() -> int:
    pid_path = state_dir() / "monitor.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        print("Codex capacity watcher is not running.")
        return 0
    if _pid_is_guard(pid):
        print(f"Codex capacity watcher is running (pid {pid}).")
    else:
        print("Codex capacity watcher is not running (stale pid file).")
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
