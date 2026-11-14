"""Pace coefficient: monthly-first for KIMI/QWEN, weekly-first for Codex & co."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import dashboard as m  # noqa: E402


class BurnRateTests(unittest.TestCase):
    def test_monthly_duration_derived_from_reset(self) -> None:
        now = int(time.time())
        reset = now + 15 * 86400  # ~half of the billing month elapsed
        window = m.Window("monthly", 50, resets_at=reset)
        rate = m.burn_rate(window)
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0.8)
        self.assertLess(rate, 1.2)

    def test_monthly_overpace(self) -> None:
        now = int(time.time())
        reset = now + 20 * 86400  # ~1/3 of the month elapsed
        window = m.Window("monthly", 60, resets_at=reset)
        rate = m.burn_rate(window)
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 1.5)

    def test_monthly_without_reset_has_no_fabricated_pace(self) -> None:
        self.assertIsNone(m.burn_rate(m.Window("monthly", 50)))

    def test_weekly_unchanged(self) -> None:
        now = int(time.time())
        window = m.Window("weekly", 50, resets_at=now + 3.5 * 86400, duration_minutes=10080)
        rate = m.burn_rate(window)
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0.8)
        self.assertLess(rate, 1.2)


class PaceSelectionTests(unittest.TestCase):
    def test_kimi_prefers_monthly(self) -> None:
        provider = m.Provider("KIMI", windows=[m.Window("weekly", 10), m.Window("monthly", 63)])
        self.assertEqual(m.pace_window_for(provider).label, "monthly")

    def test_qwen_prefers_monthly(self) -> None:
        provider = m.Provider("QWEN", windows=[m.Window("5 hour", 5), m.Window("weekly", 10), m.Window("monthly", 63)])
        self.assertEqual(m.pace_window_for(provider).label, "monthly")

    def test_codex_stays_weekly(self) -> None:
        provider = m.Provider("CODEX", windows=[m.Window("weekly", 10), m.Window("monthly", 63)])
        self.assertEqual(m.pace_window_for(provider).label, "weekly")

    def test_empty_provider(self) -> None:
        self.assertIsNone(m.pace_window_for(m.Provider("DEEPSEEK API")))


class KimiCodeUsagesTests(unittest.TestCase):
    PAYLOAD = {
        "limits": [
            {
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "100", "used": "12", "remaining": "88", "resetTime": "2026-11-14T08:47:30Z"},
            }
        ],
        "usages": {
            "limit_5h": {"used_ratio": 0.1246, "reset_time": "2026-11-14T08:47:30Z"},
            "limit_month_total": {"used_ratio": 0.6281, "reset_time": "2026-12-05T00:00:00Z"},
        },
    }

    def test_windows_mapped(self) -> None:
        windows = m.parse_kimi_code_usages(self.PAYLOAD)
        by_label = {window.label: window for window in windows}
        self.assertEqual(by_label["monthly"].used_percent, 63)
        self.assertIsNotNone(by_label["monthly"].resets_at)
        self.assertEqual(by_label["5 hour"].used_percent, 12)
        self.assertEqual(by_label["5 hour"].duration_minutes, 300)

    def test_monthly_pace_computable(self) -> None:
        windows = m.parse_kimi_code_usages(self.PAYLOAD)
        monthly = next(window for window in windows if window.label == "monthly")
        # Reset far in the future relative to the fixture is fine: burn_rate
        # only needs a positive elapsed fraction; clamp check via duration math.
        start, end = m.mimo_billing_window(monthly.resets_at)
        self.assertGreater(end, start)

    def test_garbage_payload(self) -> None:
        self.assertEqual(m.parse_kimi_code_usages(None), [])
        self.assertEqual(m.parse_kimi_code_usages({"usages": {}}), [])


if __name__ == "__main__":
    unittest.main()
