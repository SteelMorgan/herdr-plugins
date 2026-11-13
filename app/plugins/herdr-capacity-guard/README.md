# herdr-capacity-guard (Herdr App)

App instance of `../../plugins/herdr-capacity-guard`. The retry state machine
(capacity / compact-stream / DSH turn error), cooldowns, prompt-hash
suppression, composer exclusion and attention semantics are unchanged — see
the TUI README and tests.

Adapted transport:
- `agent.list` → `hc ls --json` (TUI-shaped records; `pane_id` is the terminal
  address, local `t7` or remote `dev/t3`);
- `pane.read` → `hc read <t> --lines 240`;
- `agent.prompt` + Docker mirror fallback → one `hc send <t> "Продолжить"`
  (remote machines are native addresses; no mirror route exists in the App);
- socket `events.subscribe` → `hc events --json` (snapshot + created/status/
  finished); output matching is replaced by the existing 5 s idle scan plus
  status-event inspection;
- attention labels/notifications → `herdr.guard` terminal label +
  `attention.json` in the state dir; the herdr-plugins mod renders the labels
  and posts toasts (click focuses the agent);
- fcntl//proc/SIGTERM → cross-platform FileLock, pid check and stop file.

Run on ONE central machine: `python bin/guard.py start|stop|status`. State:
`~/.local/state/herdr-app/herdr-agent-guard/` (override
`HERDR_PLUGIN_STATE_DIR`). The mod's startup hook starts it automatically.