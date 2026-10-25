#!/bin/sh
# Open or focus the MiMo TUI plugin pane.
# `plugin pane open` always splits a new pane; match by title first (title is load-bearing).
set -eu
. "$(dirname "$0")/common.sh"

require_mimo

existing=$(herdr pane list 2>/dev/null | python3 -c '
import json,sys
try:
  panes=json.load(sys.stdin).get("result", {}).get("panes", [])
except Exception:
  panes=[]
for p in panes:
  if p.get("label") == "MiMo":
    print(p.get("pane_id") or "")
    break
' 2>/dev/null || true)

if [ -n "${existing:-}" ]; then
  herdr plugin pane focus "$existing" 2>/dev/null || herdr pane focus "$existing" 2>/dev/null || true
  echo "focused existing MiMo pane $existing"
  exit 0
fi

open_tui
