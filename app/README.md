# Herdr plugins — Herdr App instance

Second instance of the repository's plugins, adapted for the **Herdr App**
(desktop `herdr-app` + herdr-core `hc`). The TUI instance in `../plugins`
is unchanged; this tree keeps the **same plugin logic** and adapts only the
differences between the two runtimes.

## TUI ↔ App mapping

| TUI (plugin v1) | Herdr App |
| --- | --- |
| `herdr-plugin.toml` panes/actions/startup | `plugin.json` + the `herdr-plugins` mod (buttons, keys, startup) |
| `herdr plugin pane open/focus` | `lib/open_pane.py` → `hc run --label herdr.space/herdr.tab/herdr.plugin` + `herdr-app do space.select/tab.select/pane.focus` |
| `herdr pane run` (inject into shell) | `hc send <terminal> "<command>"` (mimo start-here) |
| `herdr pane read/process-info` | `hc read <t> --lines N`, `hc ls --json` records |
| `herdr agent list/prompt` | `hc ls --json` status, `hc send <t> "Продолжить"` |
| Herdr socket `events.subscribe` | `hc events --json` (snapshot + `created/status/finished/labels` events) |
| Docker mirror route for remote agents | native `machine/tN` addresses (`hc --on`) |
| `pane.report_metadata` / `notification.show` | `herdr.guard` terminal label + `attention.json`, rendered/toasted by the mod |
| `[[startup]]` hooks | mod init → `restore-dashboards.py` + `guard.py start` (locked, idempotent) |
| `HERDR_PLUGIN_CONFIG_DIR` default `~/.config/herdr/plugins/config/<id>` | default `<appHome>/plugins/config/<id>` (same env override) |
| `HERDR_PLUGIN_STATE_DIR` | default `~/.local/state/herdr-app/<id>` (same env override) |
| `HERDR_PANE_ID` invoking context | `--terminal` / `$HC_TERMINAL_ID` |
| termios/tty/select keyboard | `lib/console_input.py` (POSIX identical; Windows msvcrt + VT) |
| WSL cookie bridges (`refresh-*.sh`) | `refresh_cookies.py` (native Windows; WSL interop preserved) |
| `fcntl`/`/proc`/SIGTERM | `lib/herdr_app.py` FileLock/pid/stop-file (cross-platform) |

Dashboards stay the same Python ANSI TUIs, running in App terminal panes
(adopted via labels). Their collectors, parsers, billing-window math,
renderers and key handling are preserved; on Windows the interactive
`/usage` PTY scrape is unavailable (Kimi falls back to web/API, Claude stays
disabled by the owner), and SGR mouse degrades to keyboard where the console
does not deliver mouse reports.

## Layout

```
app/
  lib/herdr_app.py        shared integration layer (hc, herdr-app, labels, locks)
  lib/console_input.py    cross-platform console input shim
  lib/open_pane.py        open-or-focus CLI used by the mod
  plugins/<name>/         per-plugin bin/ + plugin.json + README (+ tests)
  mods/herdr-plugins/     the control panel mod (actions, startup, guard attention)
  keys/keys.fragment.json recommended bindings (merged only into free keys)
  install.py              local + managed-machine installer
```

## Install

```sh
# local window machine (copies into ~/.config/herdr-app, merges keys, runs startup)
python app/install.py

# development install: registry points at this checkout
python app/install.py --link --no-startup

# managed machines (scripts only; mod/window stay local)
python app/install.py --machine dev --machine 1c

# dashboards run on a managed machine instead of this computer
python app/install.py --default-machine dev
```

The installer writes `<appHome>/herdr-plugins.json` (registry: root, python,
space, default machine, per-machine roots). The mod docks once on the right;
keep it in the layout — its init runs the startup hooks each app launch.
`herdr-app check` is run on the mod at install time.

**Guard placement:** run the agent guard on ONE central machine (the default
local install). It watches every terminal its core lists, including managed
machines, and retries Codex capacity / DSH turn errors exactly like the TUI
guard. Attention appears as the `herdr.guard` label on the terminal and as
toasts from the mod.

## Configuration

Operator configuration is outside Git, per plugin, in
`<appHome>/plugins/config/<id>/` (override with `HERDR_PLUGIN_CONFIG_DIR`):

- `herdr.quota/.env` — `DEEPSEEK_API_KEY`, optional cookie snapshots;
  `export-targets.json` for the usage.json docker export (contract unchanged:
  schema 1, `max_age_seconds` 900, consumer ERA `llm-usage`).
- `herdr.resources/hosts.toml` — optional pinned fleet (same format as the
  TUI mirror config). Without it: legacy `~/.config/herdr-mirror/hosts.toml`,
  else `hc machine ls`.
- `herdr.pve-health/config.json` — see `config.example.json` (SSH hosts,
  `windows_backup_file` accepts native Windows paths).

Secrets (`.secrets/`) stay next to the plugin root as in the TUI instance and
are never copied by the installer. State lives in `~/.local/state/herdr-app/`.

## Validation

```sh
python -m unittest discover -s app/plugins/herdr-capacity-guard/tests -v
python -m unittest discover -s app/plugins/herdr-pve-health/tests -v
herdr-app check <appHome>/mods/herdr-plugins
```

The pve-health probe tests are skipped on Windows (probe.py executes on the
remote PVE host); they run on Linux unchanged.