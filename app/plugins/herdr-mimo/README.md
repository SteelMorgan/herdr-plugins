# herdr-mimo (Herdr App)

App instance of `../../plugins/herdr-mimo`. All behaviour of the TUI shell
actions is preserved, routed through one cross-platform `bin/mimo.py`:

- `open-tui` — open or focus the MiMo terminal (label `herdr.plugin=herdr.mimo`,
  the TUI matched the pane label "MiMo"); cwd comes from the invoking terminal;
- `start-here` — types the mimo command into the invoking terminal's shell;
  refuses terminals hosting a recognized coding agent and opens the MiMo
  terminal instead (same protection as the TUI);
- `models`, `stats`, `llm-server-issue`, `llm-server-list` — thin MiMo CLI calls
  (llm-server issue prints a plaintext token once — treat output as sensitive);
- `diagnose` — binary/version, hc/herdr-app availability, App environment,
  CLI surface, llm-server tokens.

`MIMO_BIN` overrides the binary (default `~/.mimocode/bin/mimo`, `mimo.exe`
on Windows, then PATH). Invoking context: `--terminal` (the mod passes the
focused terminal) or `$HC_TERMINAL_ID`, replacing `HERDR_PANE_ID`.