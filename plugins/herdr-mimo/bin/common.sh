#!/bin/sh
# Shared helpers for herdr.mimo actions.
set -eu

MIMO_BIN="${MIMO_BIN:-/home/steel/.mimocode/bin/mimo}"
PLUGIN_ID="herdr.mimo"
ENTRYPOINT_TUI="tui"

require_mimo() {
  if [ ! -x "$MIMO_BIN" ]; then
    echo "error: mimo binary not found or not executable: $MIMO_BIN" >&2
    echo "set MIMO_BIN or install MiMoCode CLI" >&2
    exit 1
  fi
}

run_mimo() {
  require_mimo
  exec "$MIMO_BIN" "$@"
}

# Prefer the invoking pane's cwd, then workspace cwd, then stay put.
resolve_cwd() {
  if [ -n "${HERDR_PANE_ID:-}" ]; then
    cwd=$(herdr pane get "$HERDR_PANE_ID" 2>/dev/null | python3 -c '
import json,sys
try:
  d=json.load(sys.stdin)
  p=d.get("result", d).get("pane") or d.get("result", d)
  print(p.get("foreground_cwd") or p.get("cwd") or "")
except Exception:
  print("")
' 2>/dev/null || true)
    if [ -n "${cwd:-}" ] && [ -d "$cwd" ]; then
      printf '%s\n' "$cwd"
      return 0
    fi
  fi
  pwd
}

open_tui() {
  require_mimo
  cwd=$(resolve_cwd)
  args="--plugin $PLUGIN_ID --entrypoint $ENTRYPOINT_TUI --placement split --direction right --focus --cwd $cwd"
  if [ -n "${HERDR_PANE_ID:-}" ]; then
    # shellcheck disable=SC2086
    herdr plugin pane open $args --target-pane "$HERDR_PANE_ID"
  else
    # shellcheck disable=SC2086
    herdr plugin pane open $args
  fi
}

# True if the pane already has a recognized coding agent (avoid clobbering it).
pane_has_agent() {
  [ -n "${HERDR_PANE_ID:-}" ] || return 1
  herdr pane get "$HERDR_PANE_ID" 2>/dev/null | python3 -c '
import json,sys
try:
  d=json.load(sys.stdin)
  p=d.get("result", d).get("pane") or d.get("result", d)
  agent=p.get("agent") or ""
  sys.exit(0 if agent else 1)
except Exception:
  sys.exit(1)
'
}
