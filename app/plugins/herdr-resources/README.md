# herdr-resources (Herdr App)

App instance of `../../plugins/herdr-resources`. Docker/SSH collectors, the
15 s procfs probe, OOM watch, engine aggregation and the renderer are
unchanged — see the TUI README.

Adapted surface:
- fleet membership: `<appHome>/plugins/config/herdr.resources/hosts.toml`
  (same format), else legacy `~/.config/herdr-mirror/hosts.toml`, else
  `hc machine ls` (managed machines become SSH hosts);
- fleet liveness (`mirror_state`): `hc machine ls` connected states; the
  legacy herdr-mirror daemon status remains a fallback;
- container Herdr check: `remote_bin: "hc"` uses `hc ls --json`; the TUI
  `herdr session list` path is kept for containers still on the TUI;
- `restore-dashboards.py`: same bounded 60 s locked retry, same
  running/busy classification and absolute no-focus rule, but over the
  `dashboards` space and `herdr.plugin` labels; a closed dashboard terminal
  is recreated at startup (App panes end their terminal when closed);
- D-Bus autolaunch workaround stays POSIX-only.

Actions: open, diagnose. Keys: `r` refresh, `q` quit, auto 3 s.