import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recent_guard_app', Path(__file__).parents[1] / 'bin/guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class RecentCapacityTests(unittest.TestCase):
    def tail(self):
        return ('› Apply the changes\n■ ' + guard.CAPACITY_ERROR
                + '\n• Completed `/root/worker`\n\n› Ask Codex to do anything\n'
                + '  GPT-6.1-Sol high · Context 54% used\n  ? for shortcuts')

    def test_late_subagent_and_live_composer_do_not_hide_capacity(self):
        self.assertTrue(guard.has_recent_capacity(self.tail()))

    def test_error_wrapped_inside_word_is_recognized(self):
        self.assertTrue(guard.has_recent_capacity(self.tail().replace('capacity', 'capa\ncity')))

    def test_old_error_outside_eight_messages_is_ignored(self):
        self.assertFalse(guard.has_recent_capacity(self.tail().split('› Ask')[0]
                         + '\n'.join('• New result ' + str(n) for n in range(9))))

    def test_continue_supersedes_old_error(self):
        self.assertFalse(guard.has_recent_capacity(self.tail() + '\n› Продолжить\n• Working\n'))

    def test_quoted_error_in_user_message_does_not_trigger(self):
        self.assertFalse(guard.has_recent_capacity('› Explain ' + guard.CAPACITY_ERROR + '\n• Explanation'))

    def test_compact_stream_error_detected_when_wrapped_and_followed_by_completion(self):
        text = ('› Finish task\n■ Error running remote compact task: stream disconnected before\n'
                ' completion: Transport error: network error: error\ndecoding response body\n'
                '• Completed `/root/worker`\n› Ask Codex to do anything')
        self.assertEqual(guard.recent_retry_error(text), guard.COMPACT_STREAM_ERROR)

    def test_compact_stream_does_not_match_other_errors_or_user_quote(self):
        self.assertIsNone(guard.recent_retry_error('› Explain ' + guard.COMPACT_STREAM_ERROR + '\n• Explanation'))
        self.assertIsNone(guard.recent_retry_error('› Task\n■ stream disconnected before completion\n'))
        self.assertIsNone(guard.recent_retry_error('› Task\n• The log contained: ' + guard.COMPACT_STREAM_ERROR))

    def test_compact_stream_sends_once_then_waits_on_repeat(self):
        first = '› Finish task\n■ ' + guard.COMPACT_STREAM_ERROR + ' Transport error\n› Ask Codex to do anything'
        repeated = '› Продолжить\n■ ' + guard.COMPACT_STREAM_ERROR + ' Transport error\n› Ask Codex to do anything'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, '_codex_status_for_pane', return_value='idle'), patch.object(guard, 'send_continuation', return_value='hc send') as send, patch.object(guard, 'hc_cli'), patch.object(guard.time, 'time', return_value=1000):
            guard.handle_capacity_error('t7', guard.COMPACT_STREAM_ERROR, first)
            guard.handle_capacity_error('t7', guard.COMPACT_STREAM_ERROR, repeated)
            self.assertEqual(send.call_count, 1)
            state = guard.get_pane_state('t7')
            self.assertEqual(state['phase'], 'cooldown')
            self.assertEqual(state['error_kind'], 'compact_stream')
            self.assertEqual(state['retry_at'], 1600)

    def test_new_user_turn_resets_old_awaiting_attempt(self):
        old = '› First task\n■ ' + guard.CAPACITY_ERROR + '\n› Ask Codex to do anything'
        new = '› Another question\n■ ' + guard.CAPACITY_ERROR + '\n› Ask Codex to do anything'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, '_codex_status_for_pane', return_value='idle'), patch.object(guard, 'send_continuation', return_value='hc send') as send, patch.object(guard, 'hc_cli'):
            guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, old)
            guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, old)
            self.assertEqual(send.call_count, 1)
            guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, new)
            self.assertEqual(send.call_count, 2)
            state = guard.get_pane_state('t7')
            self.assertEqual(state['trigger_prompt_hash'], guard.last_submitted_prompt_hash(new))
            self.assertNotIn('Another question', str(state))

    def test_own_continue_does_not_count_as_a_new_user_turn(self):
        old = '› First task\n■ ' + guard.CAPACITY_ERROR + '\n› Ask Codex to do anything'
        after = '› Продолжить\n■ ' + guard.CAPACITY_ERROR + '\n› Ask Codex to do anything'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, '_codex_status_for_pane', return_value='idle'), patch.object(guard, 'send_continuation', return_value='hc send') as send, patch.object(guard, 'hc_cli'):
            guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, old)
            guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, after)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(guard.get_pane_state('t7')['phase'], 'cooldown')

    def test_new_user_turn_overrides_previous_cooldown(self):
        old = '› First task\n■ ' + guard.CAPACITY_ERROR
        new = '› Another question\n■ ' + guard.CAPACITY_ERROR
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, '_codex_status_for_pane', return_value='idle'), patch.object(guard, 'send_continuation', return_value='hc send') as send, patch.object(guard, 'hc_cli'):
            guard.set_pane_state('t7', {'phase': 'cooldown', 'retry_at': 999999, 'trigger_prompt_hash': guard.last_submitted_prompt_hash(old)})
            guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, new)
            send.assert_called_once()
            self.assertEqual(guard.get_pane_state('t7')['phase'], 'awaiting_capacity')

    def test_idle_and_done_after_subagent_wake_once(self):
        for status in ['idle', 'done']:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, '_codex_status_for_pane', return_value=status), patch.object(guard, 'read_pane_text', return_value=self.tail()), patch.object(guard, 'send_continuation', return_value='hc send') as send, patch.object(guard, 'hc_cli'):
                guard.process_agent_status('t7', status)
                guard.inspect_idle_pane('t7')
                send.assert_called_once_with('t7')

    def test_working_blocked_unknown_and_missing_never_receive_continue(self):
        for status in ['working', 'blocked', 'unknown', None]:
            with self.subTest(status=status), patch.object(guard, '_codex_status_for_pane', return_value=status), patch.object(guard, 'read_pane_text') as read, patch.object(guard, 'send_continuation') as send:
                guard.handle_capacity_error('t7', guard.CAPACITY_ERROR, self.tail())
                guard.inspect_idle_pane('t7')
                send.assert_not_called()
                read.assert_not_called()

    def test_done_preserves_cooldown_with_capacity_and_subagent_message(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, '_codex_status_for_pane', return_value='done'), patch.object(guard, 'read_pane_text', return_value=self.tail()), patch.object(guard, 'send_continuation') as send, patch.object(guard, 'hc_cli'):
            guard.set_pane_state('t7', {'phase':'cooldown', 'retry_at':1000})
            guard.process_agent_status('t7', 'done')
            self.assertEqual(guard.get_pane_state('t7')['retry_at'], 1000)
            send.assert_not_called()
            with patch.object(guard.time, 'time', return_value=1000):
                guard.run_scheduled_retry('t7', 1000)
            send.assert_called_once()

    def test_timer_waits_for_working_agent_then_retries_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, 'read_pane_text', return_value=self.tail()), patch.object(guard, 'send_continuation') as send, patch.object(guard, 'hc_cli'), patch.object(guard.time, 'time', return_value=1000):
            guard.set_pane_state('t7', {'phase':'cooldown','retry_at':1000})
            with patch.object(guard, '_codex_status_for_pane', return_value='working'):
                guard.run_scheduled_retry('t7', 1000)
            send.assert_not_called()
            self.assertEqual(guard.get_pane_state('t7')['phase'], 'cooldown')
            with patch.object(guard, '_codex_status_for_pane', return_value='idle'):
                guard.run_scheduled_retry('t7', 1000)
            send.assert_called_once()

    def test_periodic_scheduler_recovers_missed_output_event(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HERDR_PLUGIN_STATE_DIR=tmp), patch.object(guard, 'STOP') as stop, patch.object(guard, 'agents_snapshot', return_value=[{'pane_id':'t7','agent':'codex','agent_status':'done'}]), patch.object(guard, '_codex_status_for_pane', return_value='done'), patch.object(guard, 'read_pane_text', return_value=self.tail()), patch.object(guard, 'send_continuation', return_value='hc send') as send, patch.object(guard, 'hc_cli'):
            stop.is_set.return_value = False
            stop.wait.return_value = True
            guard.retry_scheduler()
            send.assert_called_once_with('t7')
