# herdr-quota (Herdr App)

App instance of `../../plugins/herdr-quota`. Provider collectors, parsers,
MiMo billing-month/weekly-pool math, Store/stale logic, usage.json export and
the ANSI renderer are unchanged — see the TUI README for the full contract.

Adapted surface:
- pane open/focus → `lib/open_pane.py` (labels `herdr.plugin=herdr.quota`);
- DeepSeek setup popup → a kept terminal pane (`herdr.quota.setup`), the
  masked `configure_deepseek.py` flow itself is unchanged;
- config default → `<appHome>/plugins/config/herdr.quota/.env`;
- cookie refresh → `refresh_cookies.py mimo|qwen [--auto]` (native Windows;
  WSL interop preserved), replacing the `refresh-*.sh` bridges;
- keyboard/mouse → `lib/console_input.py`; on Windows the `/usage` PTY scrape
  is unavailable (Kimi uses web/API; Claude stays disabled by the owner).

Actions (mod buttons / `herdr-plugins:quota.*` keys): open, diagnose,
configure-deepseek. Keys inside the dashboard: `r` refresh, `s` setup, `q` quit.