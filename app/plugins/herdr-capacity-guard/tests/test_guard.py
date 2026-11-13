import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "bin" / "guard.py"
SPEC = importlib.util.spec_from_file_location("capacity_guard_app", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(guard)


def codex_agent(status: str = "idle", address: str = "t7") -> dict:
    return {"pane_id": address, "agent": "codex", "agent_status": status}


def attention_entries(directory: str) -> dict:
    path = Path(directory) / guard.ATTENTION_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class PaneStateTests(unittest.TestCase):
    def test_legacy_attempts_migrate_to_waiting_for_their_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "retry-attempts.json").write_text(
                json.dumps({"t7": 1000}), encoding="utf-8"
            )
            states = guard._load_pane_states(path / guard.PANE_STATES_FILE)

        self.assertEqual(states["t7"]["phase"], "awaiting_capacity")
        self.assertEqual(states["t7"]["last_prompt_at"], 1000)


class AgentsSnapshotTests(unittest.TestCase):
    def test_hc_terminals_map_to_tui_agent_shape(self):
        listing = {
            "terminals": [
                {"address": "t7", "state": "live",
                 "status": {"app": "codex", "state": "idle", "title": ""}},
                {"address": "dev/t3", "state": "live",
                 "status": {"app": "dsh-tui", "state": "working", "title": ""}},
                {"address": "t9", "state": "live", "status": None},
                {"address": "t1", "state": "finished",
                 "status": {"app": "codex", "state": "done", "title": ""}},
            ]
        }
        with patch.object(guard, "hc_cli", return_value=listing):
            agents = guard.agents_snapshot()
        self.assertEqual(
            agents,
            [
                {"pane_id": "t7", "agent": "codex", "agent_status": "idle"},
                {"pane_id": "dev/t3", "agent": "dsh-tui", "agent_status": "working"},
            ],
        )

    def test_compact_hc_ls_shape_is_normalized(self):
        # `hc ls --json` compact records: agent = reported app, status = word.
        listing = {
            "terminals": [
                {"address": "t2", "agent": "dsh-tui", "state": "live", "status": "working"},
                {"address": "dev/t3", "agent": "dsh-tui", "state": "live", "status": "idle"},
                {"address": "t4", "agent": "C:\\Python\\python.exe", "state": "live", "status": None},
                {"address": "1c/t1", "agent": "sh", "state": "live", "status": None},
            ]
        }
        with patch.object(guard, "hc_cli", return_value=listing):
            agents = guard.agents_snapshot()
        self.assertEqual(
            agents,
            [
                {"pane_id": "t2", "agent": "dsh-tui", "agent_status": "working"},
                {"pane_id": "dev/t3", "agent": "dsh-tui", "agent_status": "idle"},
            ],
        )


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
            with (
                patch.object(guard, "agent_for_pane", return_value=codex_agent()),
                patch.object(guard, "hc_cli") as hc_cli,
            ):
                guard.handle_capacity_error("t7", guard.CAPACITY_ERROR, guard.CAPACITY_ERROR)

            sends = [c.args for c in hc_cli.call_args_list if c.args[:1] == ("send",)]
            self.assertEqual(sends, [("send", "t7", "Продолжить")])
            self.assertEqual(guard.get_pane_state("t7")["phase"], "awaiting_capacity")
            self.assertEqual(attention_entries(directory), {})

    def test_capacity_reply_after_continue_schedules_ten_minute_retry_and_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("t7", {"phase": "awaiting_capacity"})
            with (
                patch.object(guard.time, "time", return_value=1000),
                patch.object(guard, "hc_cli"),
            ):
                guard.handle_capacity_error(
                    "t7",
                    guard.CAPACITY_ERROR,
                    f"› Продолжить\n{guard.CAPACITY_ERROR}",
                )

            state = guard.get_pane_state("t7")
            self.assertEqual(state["phase"], "cooldown")
            self.assertEqual(state["retry_at"], 1000 + guard.RETRY_DELAY_SECONDS)
            entries = attention_entries(directory)
            self.assertEqual(entries["t7"]["expires_at"], 1000 + 10 * 60)
            self.assertTrue(entries["t7"]["notify"])

    def test_missing_event_context_reads_current_pane_before_scheduling(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("t7", {"phase": "awaiting_capacity"})
            with (
                patch.object(guard.time, "time", return_value=1000),
                patch.object(guard, "read_pane_text", return_value=f"› Продолжить\n{guard.CAPACITY_ERROR}") as read,
                patch.object(guard, "hc_cli"),
            ):
                guard.handle_capacity_error("t7", guard.CAPACITY_ERROR, "")

            read.assert_called_once_with("t7")
            self.assertEqual(guard.get_pane_state("t7")["phase"], "cooldown")

    def test_capacity_during_cooldown_does_not_reset_timer_or_notify_again(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            due = 1700.0
            guard.set_pane_state("t7", {"phase": "cooldown", "retry_at": due})
            with (
                patch.object(guard.time, "time", return_value=1200),
                patch.object(guard, "hc_cli") as hc_cli,
            ):
                guard.handle_capacity_error(
                    "t7",
                    guard.CAPACITY_ERROR,
                    f"› Продолжить\n{guard.CAPACITY_ERROR}",
                )

            hc_cli.assert_not_called()
            self.assertEqual(guard.get_pane_state("t7")["retry_at"], due)

    def test_stale_capacity_before_continue_does_not_start_timer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("t7", {"phase": "awaiting_capacity"})
            with (
                patch.object(guard, "read_pane_text", return_value=f"{guard.CAPACITY_ERROR}\n› Продолжить"),
                patch.object(guard, "hc_cli") as hc_cli,
            ):
                guard.handle_capacity_error(
                    "t7",
                    guard.CAPACITY_ERROR,
                    f"{guard.CAPACITY_ERROR}\n› Продолжить",
                )
            hc_cli.assert_not_called()
            self.assertEqual(guard.get_pane_state("t7")["phase"], "awaiting_capacity")

    def test_scheduled_retry_sends_at_deadline_and_clears_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("t7", {"phase": "cooldown", "retry_at": 1000.0})
            guard.report_attention("t7", "old", notify=False)
            with (
                patch.object(guard.time, "time", return_value=1000.0),
                patch.object(guard, "_codex_status_for_pane", return_value="idle"),
                patch.object(guard, "read_pane_text", return_value=guard.CAPACITY_ERROR),
                patch.object(guard, "agent_for_pane", return_value=codex_agent()),
                patch.object(guard, "hc_cli"),
            ):
                guard.run_scheduled_retry("t7", 1000.0)

            self.assertEqual(guard.get_pane_state("t7")["phase"], "awaiting_capacity")
            self.assertEqual(attention_entries(directory), {})

    def test_scheduled_retry_does_not_send_before_deadline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            due = 1001.0
            guard.set_pane_state("t7", {"phase": "cooldown", "retry_at": due})
            with (
                patch.object(guard.time, "time", return_value=1000.0),
                patch.object(guard, "send_continuation") as send,
            ):
                guard.run_scheduled_retry("t7", due)
            send.assert_not_called()

    def test_remote_machine_terminal_uses_native_hc_send(self):
        # The TUI needed a Docker mirror route for remote agents; the App
        # addresses them natively (machine/tN) through the same hc send.
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            with (
                patch.object(guard, "agent_for_pane", return_value=codex_agent("done", "dev/t5")),
                patch.object(guard, "hc_cli") as hc_cli,
            ):
                delivery = guard.send_continuation("dev/t5")
            self.assertEqual(delivery, "hc send")
            hc_cli.assert_called_once_with("send", "dev/t5", "Продолжить", timeout=90)

    def test_send_refuses_a_working_agent(self):
        with patch.object(guard, "agent_for_pane", return_value=codex_agent("working")):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                guard.send_continuation("t7")

    def test_send_refuses_when_agent_is_gone(self):
        with patch.object(guard, "agent_for_pane", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no longer recognizes"):
                guard.send_continuation("t7")

    def test_failed_send_marks_terminal_for_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            with (
                patch.object(guard, "agent_for_pane", return_value=codex_agent()),
                patch.object(guard, "hc_cli", side_effect=RuntimeError("send failed: timeout")),
            ):
                guard.handle_capacity_error("t7", guard.CAPACITY_ERROR)

            self.assertEqual(guard.get_pane_state("t7")["phase"], "delivery_failed")
            self.assertIn("t7", attention_entries(directory))

    def test_scheduled_retry_skips_a_completed_codex_agent(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ):
            guard.set_pane_state("t7", {"phase": "cooldown", "retry_at": 1000.0})
            guard.report_attention("t7", "old", notify=False)
            with (
                patch.object(guard.time, "time", return_value=1000.0),
                patch.object(guard, "_codex_status_for_pane", return_value="done"),
                patch.object(guard, "read_pane_text", return_value="• Task finished"),
                patch.object(guard, "send_continuation") as send,
                patch.object(guard, "hc_cli"),
            ):
                guard.run_scheduled_retry("t7", 1000.0)

            send.assert_not_called()
            self.assertEqual(guard.get_pane_state("t7"), {})
            self.assertEqual(attention_entries(directory), {})

    def test_completed_agent_clears_pending_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ), patch.object(guard, "hc_cli"), patch.object(guard, "read_pane_text", return_value="• Task finished"):
            guard.set_pane_state("t7", {"phase": "cooldown", "retry_at": 1000.0})
            guard.report_attention("t7", "old", notify=False)
            guard.process_agent_status("t7", "done")

            self.assertEqual(guard.get_pane_state("t7"), {})
            self.assertEqual(attention_entries(directory), {})

    def test_unrelated_text_does_not_prompt_or_notify(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}
        ), patch.object(guard, "hc_cli") as hc_cli:
            guard.handle_capacity_error("t7", "Please try again later.")
            hc_cli.assert_not_called()


class MonitorTests(unittest.TestCase):
    def test_monitor_reconnects_and_reconciles_watched_terminals(self):
        class StopAfterTwoWaits:
            waits = 0

            def is_set(self):
                return False

            def wait(self, timeout):
                self.waits += 1
                return self.waits >= 2

        snapshots = [
            [{"pane_id": "t7", "agent": "codex", "agent_status": "idle"}],
            [{"pane_id": "dev/t3", "agent": "dsh-tui", "agent_status": "idle"}],
        ]

        def fake_stream():
            yield "snapshot", "", {"terminals": []}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}),
            patch.object(guard, "STOP", StopAfterTwoWaits()),
            patch.object(guard, "event_stream", side_effect=[fake_stream(), fake_stream()]) as stream,
            patch.object(guard, "agents_snapshot", side_effect=snapshots),
            patch.object(guard, "start_pane_watcher") as start_watcher,
            patch.object(guard, "start_retry_scheduler"),
        ):
            self.assertEqual(guard.monitor(), 0)

        self.assertEqual(stream.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in start_watcher.call_args_list],
            ["t7", "dev/t3"],
        )

    def test_status_event_inspects_ready_agent(self):
        data = {"status": {"app": "codex", "state": "done", "title": ""}}
        with (
            patch.object(guard, "start_pane_watcher"),
            patch.object(guard, "process_agent_status") as process,
        ):
            guard.process_lifecycle_event("status", "t7", data)
        process.assert_called_once_with("t7", "done")


if __name__ == "__main__":
    unittest.main()
