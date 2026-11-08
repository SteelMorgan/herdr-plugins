import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "bin" / "guard.py"
SPEC = importlib.util.spec_from_file_location("capacity_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(guard)


class PaneStateTests(unittest.TestCase):
    def test_legacy_attempts_migrate_to_waiting_for_their_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "retry-attempts.json").write_text(
                json.dumps({"w1:p1": 1000}), encoding="utf-8"
            )
            states = guard._load_pane_states(path / guard.PANE_STATES_FILE)

        self.assertEqual(states["w1:p1"]["phase"], "awaiting_capacity")
        self.assertEqual(states["w1:p1"]["last_prompt_at"], 1000)


class CapacityHandlingTests(unittest.TestCase):
    def setUp(self):
        status = patch.object(guard, "_codex_status_for_pane", return_value="idle")
        status.start()
        self.addCleanup(status.stop)
        read = patch.object(guard, "read_pane_text", return_value=guard.CAPACITY_ERROR)
        read.start()
        self.addCleanup(read.stop)

    def test_first_capacity_error_sends_immediately(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            calls = []

            def fake_api_request(method, params):
                calls.append((method, params))
                return {"result": {}}

            with patch.object(guard, "api_request", side_effect=fake_api_request):
                guard.handle_capacity_error(
                    "w1:p1", guard.CAPACITY_ERROR, guard.CAPACITY_ERROR
                )

            prompts = [params for method, params in calls if method == "agent.prompt"]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["text"], "Продолжить")
            self.assertEqual(guard.get_pane_state("w1:p1")["phase"], "awaiting_capacity")
            self.assertFalse(any(method == "notification.show" for method, _ in calls))

    def test_capacity_reply_after_continue_schedules_ten_minute_retry_and_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("w1:p1", {"phase": "awaiting_capacity"})
            calls = []

            def fake_api_request(method, params):
                calls.append((method, params))
                return {"result": {}}

            with (
                patch.object(guard.time, "time", return_value=1000),
                patch.object(guard, "api_request", side_effect=fake_api_request),
            ):
                guard.handle_capacity_error(
                    "w1:p1",
                    guard.CAPACITY_ERROR,
                    f"› Продолжить\n{guard.CAPACITY_ERROR}",
                )

            state = guard.get_pane_state("w1:p1")
            self.assertEqual(state["phase"], "cooldown")
            self.assertEqual(state["retry_at"], 1000 + guard.RETRY_DELAY_SECONDS)
            metadata = [params for method, params in calls if method == "pane.report_metadata"]
            self.assertEqual(metadata[0]["ttl_ms"], 10 * 60 * 1000)
            self.assertTrue(any(method == "notification.show" for method, _ in calls))

    def test_missing_event_context_reads_current_pane_before_scheduling(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("w1:p1", {"phase": "awaiting_capacity"})
            with (
                patch.object(guard.time, "time", return_value=1000),
                patch.object(guard, "read_pane_text", return_value=f"› Продолжить\n{guard.CAPACITY_ERROR}") as read,
                patch.object(guard, "api_request"),
            ):
                guard.handle_capacity_error("w1:p1", guard.CAPACITY_ERROR, "")

            read.assert_called_once_with("w1:p1")
            self.assertEqual(guard.get_pane_state("w1:p1")["phase"], "cooldown")

    def test_capacity_during_cooldown_does_not_reset_timer_or_notify_again(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            due = 1700.0
            guard.set_pane_state("w1:p1", {"phase": "cooldown", "retry_at": due})
            with (
                patch.object(guard.time, "time", return_value=1200),
                patch.object(guard, "api_request") as request,
            ):
                guard.handle_capacity_error(
                    "w1:p1",
                    guard.CAPACITY_ERROR,
                    f"› Продолжить\n{guard.CAPACITY_ERROR}",
                )

            request.assert_not_called()
            self.assertEqual(guard.get_pane_state("w1:p1")["retry_at"], due)

    def test_stale_capacity_before_continue_does_not_start_timer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("w1:p1", {"phase": "awaiting_capacity"})
            with (
                patch.object(guard, "read_pane_text", return_value=f"{guard.CAPACITY_ERROR}\n› Продолжить"),
                patch.object(guard, "api_request") as request,
            ):
                guard.handle_capacity_error(
                    "w1:p1",
                    guard.CAPACITY_ERROR,
                    f"{guard.CAPACITY_ERROR}\n› Продолжить",
                )
            request.assert_not_called()
            self.assertEqual(guard.get_pane_state("w1:p1")["phase"], "awaiting_capacity")

    def test_scheduled_retry_sends_at_deadline_and_clears_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("w1:p1", {"phase": "cooldown", "retry_at": 1000.0})
            calls = []

            def fake_api_request(method, params):
                calls.append((method, params))
                return {"result": {}}

            with (
                patch.object(guard.time, "time", return_value=1000.0),
                patch.object(guard, "_codex_status_for_pane", return_value="idle"),
                patch.object(guard, "read_pane_text", return_value=guard.CAPACITY_ERROR),
                patch.object(guard, "send_continuation", return_value="agent API"),
                patch.object(guard, "api_request", side_effect=fake_api_request),
            ):
                guard.run_scheduled_retry("w1:p1", 1000.0)

            self.assertEqual(guard.get_pane_state("w1:p1")["phase"], "awaiting_capacity")
            self.assertTrue(
                any(
                    method == "pane.report_metadata" and params.get("clear_state_labels")
                    for method, params in calls
                )
            )

    def test_scheduled_retry_does_not_send_before_deadline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            due = 1001.0
            guard.set_pane_state("w1:p1", {"phase": "cooldown", "retry_at": due})
            with (
                patch.object(guard.time, "time", return_value=1000.0),
                patch.object(guard, "send_continuation") as send,
            ):
                guard.run_scheduled_retry("w1:p1", due)
            send.assert_not_called()

    def test_mirror_agent_not_ready_falls_back_to_terminal_input(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            calls = []

            def fake_api_request(method, params):
                calls.append((method, params))
                if method == "agent.prompt":
                    raise RuntimeError("agent_not_ready: mirrored foreground process")
                if method == "agent.list":
                    return {
                        "result": {"agents": [{"pane_id": "w1:p1", "agent": "codex"}]}
                    }
                return {"result": {}}

            cli_calls = []

            def fake_run(command, **kwargs):
                cli_calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(guard, "api_request", side_effect=fake_api_request),
                patch.object(guard.shutil, "which", return_value="/usr/bin/herdr"),
                patch.object(guard.subprocess, "run", side_effect=fake_run),
            ):
                guard.handle_capacity_error("w1:p1", guard.CAPACITY_ERROR)

            self.assertEqual(
                cli_calls,
                [
                    ["/usr/bin/herdr", "pane", "send-text", "w1:p1", "Продолжить"],
                    ["/usr/bin/herdr", "pane", "send-keys", "w1:p1", "enter"],
                ],
            )
            self.assertFalse(any(method == "notification.show" for method, _ in calls))

    def test_docker_mirror_uses_remote_agent_api(self):
        route = ('docker', '1c-ai-sandbox', ['/usr/sbin/runuser', '-u', 'vscode', '--', '/home/vscode/.local/bin/herdr'], 'w1:pCC')
        info = {'result': {'process_info': {'foreground_processes': [{'argv': ['/opt/herdr-mirror', 'pane', '1c-ai-sandbox', 'w1:pCC', '--remote-bin', '/usr/sbin/runuser -u vscode -- /home/vscode/.local/bin/herdr', '--container', '1c-ai-sandbox', '--docker-bin', 'docker']}]}}}
        self.assertEqual(guard.docker_mirror_route.__name__, 'docker_mirror_route')
        def api(method, params):
            if method == 'agent.prompt': raise RuntimeError('agent_not_ready: mirrored foreground')
            if method == 'agent.list': return {'result': {'agents': [{'pane_id': 'w12:p9V', 'agent': 'codex'}]}}
            if method == 'pane.process_info': return info
            raise AssertionError(method)
        outputs = [
            subprocess.CompletedProcess([], 0, json.dumps({'result': {'agents': [{'pane_id': 'w1:pCC', 'agent': 'codex', 'agent_status': 'done'}]}}), ''),
            subprocess.CompletedProcess([], 0, json.dumps({'result': {'type': 'agent_prompted'}}), ''),
        ]
        with patch.object(guard, 'api_request', side_effect=api), patch.object(guard.subprocess, 'run', side_effect=outputs) as run:
            self.assertEqual(guard.send_continuation('w12:p9V'), 'remote Docker agent API')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], [*route[:1], 'exec', route[1], *route[2], 'agent', 'list'])
        self.assertEqual(run.call_args_list[1].args[0][-4:], ['agent', 'prompt', 'w1:pCC', 'Продолжить'])

    def test_docker_mirror_never_sends_to_working_agent(self):
        output = subprocess.CompletedProcess([], 0, json.dumps({'result': {'agents': [{'pane_id': 'w1:pCC', 'agent': 'codex', 'agent_status': 'working'}]}}), '')
        with patch.object(guard.subprocess, 'run', return_value=output) as run:
            with self.assertRaisesRegex(RuntimeError, 'not a ready Codex'):
                guard.send_docker_mirror(('docker', '1c-ai-sandbox', ['herdr'], 'w1:pCC'))
        self.assertEqual(run.call_count, 1)

    def test_docker_mirror_failure_never_falls_back_to_terminal(self):
        with patch.object(guard, 'api_request', side_effect=[RuntimeError('agent_not_ready: mirror'), {'result': {'agents': [{'pane_id': 'w1:p1', 'agent': 'codex'}]}}]), patch.object(guard, 'docker_mirror_route', return_value=('docker', 'box', ['herdr'], 'w1:p1')), patch.object(guard, 'send_docker_mirror', side_effect=RuntimeError('timeout')), patch.object(guard.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                guard.send_continuation('w1:p1')
        run.assert_not_called()

    def test_failed_mirror_send_marks_pane_for_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            prompt_calls = []

            def fake_api_request(method, params):
                if method == "agent.prompt":
                    prompt_calls.append(params)
                    raise RuntimeError("agent_not_ready: mirrored foreground process")
                if method == "agent.list":
                    return {
                        "result": {"agents": [{"pane_id": "w1:p1", "agent": "codex"}]}
                    }
                return {"result": {}}

            def failed_run(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, "", "terminal unavailable")

            with (
                patch.object(guard, "api_request", side_effect=fake_api_request),
                patch.object(guard.shutil, "which", return_value="/usr/bin/herdr"),
                patch.object(guard.subprocess, "run", side_effect=failed_run),
            ):
                guard.handle_capacity_error("w1:p1", guard.CAPACITY_ERROR)

            self.assertEqual(len(prompt_calls), 1)
            self.assertEqual(guard.get_pane_state("w1:p1")["phase"], "delivery_failed")

    def test_mirror_fallback_requires_codex_still_present(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            calls = []

            def fake_api_request(method, params):
                calls.append((method, params))
                if method == "agent.prompt":
                    raise RuntimeError("agent_not_ready: mirrored foreground process")
                if method == "agent.list":
                    return {"result": {"agents": []}}
                return {"result": {}}

            with (
                patch.object(guard, "api_request", side_effect=fake_api_request),
                patch.object(guard.shutil, "which", return_value="/usr/bin/herdr"),
                patch.object(guard.subprocess, "run") as run,
            ):
                guard.handle_capacity_error("w1:p1", guard.CAPACITY_ERROR)

            run.assert_not_called()
            self.assertTrue(any(method == "notification.show" for method, _ in calls))

    def test_scheduled_retry_skips_a_completed_codex_agent(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("w1:p1", {"phase": "cooldown", "retry_at": 1000.0})
            with (
                patch.object(guard.time, "time", return_value=1000.0),
                patch.object(guard, "_codex_status_for_pane", return_value="done"),
                patch.object(guard, "read_pane_text", return_value="• Task finished"),
                patch.object(guard, "send_continuation") as send,
                patch.object(guard, "api_request") as request,
            ):
                guard.run_scheduled_retry("w1:p1", 1000.0)

            send.assert_not_called()
            self.assertEqual(guard.get_pane_state("w1:p1"), {})
            self.assertTrue(
                any(
                    call.args[0] == "pane.report_metadata"
                    and call.args[1].get("clear_state_labels")
                    for call in request.call_args_list
                )
            )

    def test_completed_agent_clears_pending_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ), patch.object(guard, "api_request") as request, patch.object(guard, "read_pane_text", return_value="• Task finished"):
            guard.set_pane_state("w1:p1", {"phase": "cooldown", "retry_at": 1000.0})
            guard.process_agent_status("w1:p1", "done")

            self.assertEqual(guard.get_pane_state("w1:p1"), {})
            self.assertTrue(
                any(
                    call.args[0] == "pane.report_metadata"
                    and call.args[1].get("clear_state_labels")
                    for call in request.call_args_list
                )
            )

    def test_unrelated_text_does_not_prompt_or_notify(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ), patch.object(guard, "api_request") as request:
            guard.handle_capacity_error("w1:p1", "Please try again later.")
            request.assert_not_called()

    def test_pane_watcher_reconnects_after_event_stream_closes(self):
        class FakeSocket:
            def close(self):
                pass

            def shutdown(self, how):
                pass

        class StopAfterTwoWaits:
            waits = 0

            def is_set(self):
                return False

            def wait(self, timeout):
                self.waits += 1
                return self.waits >= 2

        event = json.dumps(
            {
                "event": "pane.output_matched",
                "data": {"matched_line": guard.CAPACITY_ERROR, "read": {"text": ""}},
            }
        ).encode() + b"\n"
        subscriptions = [(FakeSocket(), BytesIO(b"")), (FakeSocket(), BytesIO(event))]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}),
            patch.object(guard, "STOP", StopAfterTwoWaits()),
            patch.object(guard, "subscribe", side_effect=subscriptions) as subscribe,
            patch.object(guard, "handle_capacity_error") as handle,
            patch.object(guard, "read_pane_text", return_value=""),
        ):
            guard.watch_pane("w1:p1")

        self.assertEqual(subscribe.call_count, 2)
        handle.assert_called_once_with("w1:p1", guard.CAPACITY_ERROR, "")
        self.assertNotIn("w1:p1", guard.WATCHER_SOCKETS)

    def test_supervisor_reconnects_and_reconciles_codex_panes(self):
        class FakeSocket:
            def close(self):
                pass

        class StopAfterTwoWaits:
            waits = 0

            def is_set(self):
                return False

            def wait(self, timeout):
                self.waits += 1
                return self.waits >= 2

        snapshots = [
            [{"pane_id": "w1:p1", "agent": "codex"}],
            [{"pane_id": "w1:p2", "agent": "codex"}],
        ]
        subscriptions = [(FakeSocket(), BytesIO(b"")), (FakeSocket(), BytesIO(b""))]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(guard, "STOP", StopAfterTwoWaits()),
            patch.object(guard, "state_dir", return_value=Path(directory)),
            patch.object(guard, "subscribe", side_effect=subscriptions) as subscribe,
            patch.object(guard, "agents_snapshot", side_effect=snapshots),
            patch.object(guard, "start_pane_watcher") as start_watcher,
            patch.object(guard, "start_retry_scheduler"),
        ):
            self.assertEqual(guard.monitor(), 0)

        self.assertEqual(subscribe.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in start_watcher.call_args_list],
            ["w1:p1", "w1:p2"],
        )


if __name__ == "__main__":
    unittest.main()
