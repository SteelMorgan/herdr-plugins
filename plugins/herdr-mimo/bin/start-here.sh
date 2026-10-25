#!/bin/sh
# Launch mimo TUI in the invoking pane. Falls back to the plugin TUI pane.
# Refuses to take over a pane that already hosts a recognized coding agent.
set -eu
. "$(dirname "$0")/common.sh"

require_mimo

if [ -n "${HERDR_PANE_ID:-}" ] && command -v herdr >/dev/null 2>&1; then
  if pane_has_agent; then
    echo "refusing: pane $HERDR_PANE_ID already hosts a coding agent; opening MiMo TUI pane instead" >&2
    open_tui
    exit 0
  fi
  exec herdr pane run "$HERDR_PANE_ID" "$MIMO_BIN"
fi

open_tui
