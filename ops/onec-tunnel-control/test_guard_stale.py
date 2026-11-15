"""Offline tests for the idle-orphan stale criterion (incident 2026-11-15)."""
import importlib.util
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'guard', Path(__file__).with_name('onec-ssh-tunnel-guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

NOW = time.time()


def snap(lastrcv=1000, lastack=1000, send_queue=0, backoff=0, started=None):
    return {'state': 'observed',
            'identity': {'pid': 1, 'uid': 999, 'comm': 'sshd-session', 'start_ticks': '1'},
            'transport': {'lastrcv': lastrcv, 'lastack': lastack,
                          'send_queue': send_queue, 'backoff': backoff},
            'key': 'k', 'session_started_epoch': started if started is not None else NOW - 3600}


class Stale(unittest.TestCase):
    def test_legacy_dead_client_branch(self):
        s = snap(lastrcv=200000, lastack=200000, send_queue=5, backoff=3)
        self.assertTrue(guard.stale(s))
        self.assertTrue(guard.stale(s, NOW))

    def test_idle_orphan_with_container_start(self):
        s = snap(lastrcv=200000, lastack=200000, started=NOW - 3600)
        self.assertTrue(guard.stale(s, NOW - 100))

    def test_idle_orphan_without_flag_untouched(self):
        s = snap(lastrcv=200000, lastack=200000, started=NOW - 3600)
        self.assertFalse(guard.stale(s))

    def test_healthy_idle_keepalive_fresh(self):
        s = snap(lastrcv=1000, lastack=1000, started=NOW - 3600)
        self.assertFalse(guard.stale(s, NOW - 100))

    def test_session_newer_than_container_untouched(self):
        s = snap(lastrcv=200000, lastack=200000, started=NOW - 50)
        self.assertFalse(guard.stale(s, NOW - 100))

    def test_unobserved_untouched(self):
        s = snap(lastrcv=200000, lastack=200000)
        s['state'] = 'transport_identity_unproven'
        self.assertFalse(guard.stale(s, NOW - 100))

    def test_confirmations_and_cooldown(self):
        s = snap(lastrcv=200000, lastack=200000, started=NOW - 3600)
        prev, reap = guard.decide(s, {}, 0, NOW - 100)
        self.assertFalse(reap)
        prev, reap = guard.decide(s, prev, 20, NOW - 100)
        self.assertFalse(reap)
        prev, reap = guard.decide(s, prev, 45, NOW - 100)
        self.assertTrue(reap)
        prev['last_termination'] = 45  # main() records it after terminate()
        prev, reap = guard.decide(s, prev, 50, NOW - 100)
        self.assertFalse(reap)  # cooldown
        self.assertEqual(prev['state'], 'cooldown')


if __name__ == '__main__':
    unittest.main()
