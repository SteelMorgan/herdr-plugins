"""Scan backoff: 30s healthy, 300s while any managed machine is down."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import guard as g  # noqa: E402


class ScanIntervalTests(unittest.TestCase):
    def test_all_connected(self) -> None:
        machines = [{"name": "dev", "state": "connected"}, {"name": "1c", "state": "connected"}]
        self.assertEqual(g.scan_interval(machines), g.SCAN_INTERVAL_SECONDS)
        self.assertEqual(g.SCAN_INTERVAL_SECONDS, 30)

    def test_unreachable_backs_off(self) -> None:
        machines = [{"name": "dev", "state": "unreachable"}, {"name": "1c", "state": "connected"}]
        self.assertEqual(g.scan_interval(machines), g.SCAN_INTERVAL_DEGRADED_SECONDS)
        self.assertEqual(g.SCAN_INTERVAL_DEGRADED_SECONDS, 300)

    def test_temporary_machines_ignored(self) -> None:
        machines = [{"name": "tmp", "state": "unreachable", "temporary": True}]
        self.assertEqual(g.scan_interval(machines), g.SCAN_INTERVAL_SECONDS)

    def test_empty_fleet(self) -> None:
        self.assertEqual(g.scan_interval([]), g.SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main()
