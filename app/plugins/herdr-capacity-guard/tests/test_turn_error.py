import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "bin" / "guard.py"
SPEC = importlib.util.spec_from_file_location("turn_guard_app", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(guard)


def turn_tail(error: bool = True) -> str:
    body = (
        "⚓ Thinking · 2s\n"
        "• Bash(hc read t7 --lines 50; git diff --stat) · 0s\n"
        " ⎿  ⠹ Thinking…\n"
    )
    if error:
        body += (
            "────────────────────────────── turn error · The engine is currently overloaded, "
            "please try again later ────────────────────────────── ▾\n"
        )
    return (
        body
        + "\n"
        + "  ⏳ Queued · delivered after the turn\n"
        + "    ↳ [HR · dev/t1] LIVE GO по APPROVE_SCRIPT\n"
        + "  Alt+↑ Retract · Esc interrupts and holds the queue\n"
        + "\n"
        + "╭──────────────────────────────────────────────────────────────────────╮\n"
        + "⌸ ❯                                                                    ⛶\n"
        + "╰──────────────────────────────────────────────────────────────────────╯\n"
        + " k3.1-pro · ▄▆█▁▅▄ 48 tps · low · cache 99.8% · hr-agent-integration\n"
    )


def api_error_tail() -> str:
    """DSH divider for a provider API failure, e.g. kimi-token-plan 400."""
    return turn_tail().replace(
        "The engine is currently overloaded, please try again later",
        'kimi-token-plan API error (400): {"code":"invalid_request_error",'
        '"message":"Invalid request: tokenization failed","type":"invalid_request_error"}',
    )


def dsh_agent(status: str = "idle") -> dict:
    return {"pane_id": "dev/t3", "agent": "dsh-tui", "agent_status": status}


class TurnErrorDetectionTests(unittest.TestCase):
    def test_current_turn_error_is_recognized(self):
        self.assertIsNotNone(guard.recent_turn_error(turn_tail()))

    def test_kimi_token_plan_api_error_is_recognized(self):
        self.assertIsNotNone(guard.recent_turn_error(api_error_tail()))

    def test_later_activity_makes_the_error_stale(self):
        self.assertIsNone(guard.recent_turn_error(turn_tail() + "\n⚓ Thinking · 1s\n"))

    def test_new_assistant_text_makes_the_error_stale(self):
        self.assertIsNone(guard.recent_turn_error(turn_tail() + "\n● Готово.\n"))

    def test_submitted_continue_and_new_turn_make_the_error_stale(self):
        self.assertIsNone(
            guard.recent_turn_error(turn_tail() + "\n❯ Продолжить\n⚓ Thinking · 1s\n")
        )

    def test_a_new_submitted_prompt_makes_the_error_stale_even_before_output(self):
        self.assertIsNone(guard.recent_turn_error(turn_tail() + "\n❯ New request\n"))

    def test_own_continue_does_not_hide_the_error_until_the_retry_produces_output(self):
        self.assertIsNotNone(guard.recent_turn_error(turn_tail() + "\n❯ Продолжить\n"))
        self.assertIsNone(
            guard.recent_turn_error(turn_tail() + "\n❯ Продолжить\n● Готово.\n")
        )

    def test_other_turn_errors_are_retried(self):
        text = turn_tail().replace(
            "The engine is currently overloaded, please try again later",
            "Authentication failed",
        )
        self.assertIsNotNone(guard.recent_turn_error(text))


class TurnErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        status = patch.object(guard, "agent_for_pane", return_value=dsh_agent())
        status.start()
        self.addCleanup(status.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_first_turn_error_sends_continue_and_starts_cooldown(self):
        calls = []

        def fake_send(pane_id, expected_kind="codex"):
            calls.append((pane_id, expected_kind))
            return "hc send"

        with (
            patch.object(guard.time, "time", return_value=1000.0),
            patch.object(guard, "send_continuation", side_effect=fake_send),
            patch.object(guard, "hc_cli"),
        ):
            guard.handle_turn_error("dev/t3", "turn error", turn_tail())

        self.assertEqual(calls, [("dev/t3", "dsh-tui")])
        state = guard.get_pane_state("dev/t3")
        self.assertEqual(state["phase"], "turn_cooldown")
        self.assertEqual(state["retry_at"], 1000.0 + guard.RETRY_DELAY_SECONDS)

    def test_kimi_api_error_sends_continue_and_starts_cooldown(self):
        calls = []

        def fake_send(pane_id, expected_kind="codex"):
            calls.append((pane_id, expected_kind))
            return "hc send"

        with (
            patch.object(guard.time, "time", return_value=1000.0),
            patch.object(guard, "send_continuation", side_effect=fake_send),
            patch.object(guard, "hc_cli"),
        ):
            guard.handle_turn_error("dev/t3", "turn error", api_error_tail())

        self.assertEqual(calls, [("dev/t3", "dsh-tui")])
        self.assertEqual(guard.get_pane_state("dev/t3")["phase"], "turn_cooldown")

    def test_duplicate_turn_error_during_cooldown_does_not_send_again(self):
        guard.set_pane_state(
            "dev/t3",
            {"phase": "turn_cooldown", "retry_at": 2000.0, "agent_kind": "dsh-tui"},
        )
        with (
            patch.object(guard.time, "time", return_value=1000.0),
            patch.object(guard, "send_continuation") as send,
            patch.object(guard, "hc_cli"),
        ):
            guard.handle_turn_error("dev/t3", "turn error", turn_tail())

        send.assert_not_called()

    def test_working_agent_is_not_prompted(self):
        with (
            patch.object(guard, "agent_for_pane", return_value=dsh_agent("working")),
            patch.object(guard, "send_continuation") as send,
        ):
            guard.handle_turn_error("dev/t3", "turn error", turn_tail())

        send.assert_not_called()

    def test_scheduled_retry_prompts_while_error_is_current(self):
        guard.set_pane_state(
            "dev/t3",
            {"phase": "turn_cooldown", "retry_at": 1000.0, "agent_kind": "dsh-tui"},
        )
        with (
            patch.object(guard.time, "time", return_value=1000.0),
            patch.object(guard, "read_pane_text", return_value=turn_tail()),
            patch.object(guard, "send_continuation", return_value="hc send") as send,
            patch.object(guard, "hc_cli"),
        ):
            guard.run_turn_retry("dev/t3", 1000.0)

        send.assert_called_once_with("dev/t3", "dsh-tui")
        self.assertEqual(guard.get_pane_state("dev/t3")["phase"], "turn_cooldown")

    def test_scheduled_retry_waits_for_a_working_agent(self):
        guard.set_pane_state(
            "dev/t3",
            {"phase": "turn_cooldown", "retry_at": 1000.0, "agent_kind": "dsh-tui"},
        )
        with (
            patch.object(guard.time, "time", return_value=1000.0),
            patch.object(guard, "agent_for_pane", return_value=dsh_agent("working")),
            patch.object(guard, "send_continuation") as send,
            patch.object(guard, "read_pane_text") as read,
            patch.object(guard, "hc_cli"),
        ):
            guard.run_turn_retry("dev/t3", 1000.0)

        send.assert_not_called()
        read.assert_not_called()
        self.assertEqual(guard.get_pane_state("dev/t3")["phase"], "turn_cooldown")

    def test_scheduled_retry_clears_when_error_is_gone(self):
        guard.set_pane_state(
            "dev/t3",
            {"phase": "turn_cooldown", "retry_at": 1000.0, "agent_kind": "dsh-tui"},
        )
        with (
            patch.object(guard.time, "time", return_value=1000.0),
            patch.object(guard, "read_pane_text", return_value=turn_tail(error=False)),
            patch.object(guard, "send_continuation") as send,
            patch.object(guard, "hc_cli"),
        ):
            guard.run_turn_retry("dev/t3", 1000.0)

        send.assert_not_called()
        self.assertEqual(guard.get_pane_state("dev/t3"), {})

    def test_reconcile_starts_a_dsh_tui_watcher(self):
        with patch.object(
            guard,
            "agents_snapshot",
            return_value=[dsh_agent()],
        ), patch.object(guard, "start_pane_watcher") as start:
            guard.reconcile_watched_panes()
        start.assert_called_once_with("dev/t3")

    def test_lifecycle_status_event_starts_a_dsh_tui_watcher(self):
        data = {"status": {"app": "dsh-tui", "state": "idle", "title": ""}}
        with patch.object(guard, "start_pane_watcher") as start, patch.object(
            guard, "process_agent_status"
        ):
            guard.process_lifecycle_event("status", "dev/t3", data)
        start.assert_called_once_with("dev/t3")

    def test_finished_event_clears_state_and_attention(self):
        guard.set_pane_state("dev/t3", {"phase": "turn_cooldown", "retry_at": 1.0})
        with patch.object(guard, "hc_cli"):
            guard.process_lifecycle_event("finished", "dev/t3", {"exit_code": 0})
        self.assertEqual(guard.get_pane_state("dev/t3"), {})
        self.assertNotIn("dev/t3", guard.WATCHING)

    def test_dsh_turn_error_is_not_treated_as_codex_capacity(self):
        with (
            patch.object(guard, "_codex_status_for_pane", return_value=None),
            patch.object(guard, "agent_for_pane", return_value=dsh_agent()),
            patch.object(guard, "read_pane_text", return_value=turn_tail()),
            patch.object(guard, "send_continuation", return_value="hc send") as send,
            patch.object(guard, "hc_cli"),
        ):
            guard.inspect_idle_pane("dev/t3")

        send.assert_called_once_with("dev/t3", "dsh-tui")


if __name__ == "__main__":
    unittest.main()
