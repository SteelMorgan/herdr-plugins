# herdr.mimo

MVP Herdr plugin for [MiMoCode CLI](https://mimo.xiaomi.com) (`mimo`).

## What it does

| Surface | Action / pane | Result |
|---|---|---|
| Pane | `tui` (title **MiMo**) | Split pane running the MiMo TUI |
| Action | `open-tui` | Focus existing MiMo pane by title, else open split TUI pane |
| Action | `start-here` | Run `mimo` in the invoking pane; if that pane already hosts a coding agent, open TUI pane instead |
| Action | `llm-server-issue` | Mint a scoped LLM-server token + base URL |
| Action | `llm-server-list` | List tokens for this directory |
| Action | `models` | List configured models |
| Action | `stats` | Token usage / cost stats |
| Action | `diagnose` | Binary, version, Herdr env, CLI surface |

Plugin actions are async: `herdr plugin action invoke` returns a `log_id`; read stdout/stderr from `herdr plugin log list --plugin herdr.mimo`.

## Install (local)

```bash
herdr plugin link /home/steel/projects/herdr-plugins/plugins/herdr-mimo --enabled
herdr plugin action list --plugin herdr.mimo
```

## Usage

```bash
herdr plugin action invoke herdr.mimo.open-tui
herdr plugin action invoke herdr.mimo.start-here
herdr plugin action invoke herdr.mimo.llm-server-issue
herdr plugin action invoke herdr.mimo.diagnose
herdr plugin pane open --plugin herdr.mimo --entrypoint tui --placement split
```

Optional keybinding in `~/.config/herdr/config.toml` (then `herdr server reload-config`):

```toml
[[keys.command]]
key = "m"
# pick an unused prefix combo — do not collide with herdr builtins
type = "plugin_action"
plugin = "herdr.mimo"
action = "open-tui"
```

## Notes

- Pane command uses absolute `MIMO_BIN` (default `/home/steel/.mimocode/bin/mimo`) because the Herdr server may exec with a minimal `PATH`.
- Override with `MIMO_BIN` if the CLI lives elsewhere.
- `open-tui` matches an existing pane by label `MiMo` (plugin `open` always splits a new pane).
- TUI cwd is taken from the invoking pane when possible, else the workspace cwd.
- `start-here` refuses to run over a pane that already has a recognized coding agent (`herdr pane run` would inject into that agent).
- Agent detection (`herdr agent list` / `agent prompt`) is **not** in this MVP — that needs a Herdr integration / core kind for `mimo`.
- Action logs capture stdout/stderr; `llm-server-issue` prints a plaintext token once — treat logs as sensitive.

## Layout

```
herdr-mimo/
  herdr-plugin.toml
  bin/
    common.sh
    open-tui.sh
    start-here.sh
    llm-server-issue.sh
    llm-server-list.sh
    models.sh
    stats.sh
    diagnose.sh
```
