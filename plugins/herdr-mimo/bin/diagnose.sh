#!/bin/sh
# Diagnose mimo + Herdr integration surface for this plugin.
set -eu
. "$(dirname "$0")/common.sh"

echo "herdr.mimo diagnose"
echo "==================="
echo "MIMO_BIN=$MIMO_BIN"
if [ -x "$MIMO_BIN" ]; then
  echo "mimo_binary: ok"
  echo -n "mimo_version: "
  "$MIMO_BIN" --version 2>/dev/null || echo "(no --version)"
else
  echo "mimo_binary: MISSING"
fi

echo "herdr_cli: $(command -v herdr || echo MISSING)"
echo "HERDR_ENV=${HERDR_ENV:-}"
echo "HERDR_PANE_ID=${HERDR_PANE_ID:-}"
echo "HERDR_SOCKET_PATH=${HERDR_SOCKET_PATH:-}"
echo "HERDR_PLUGIN_ROOT=${HERDR_PLUGIN_ROOT:-}"
echo "HERDR_PLUGIN_CONFIG_DIR=${HERDR_PLUGIN_CONFIG_DIR:-}"
echo "cwd=$(pwd)"

echo
echo "mimo CLI surface:"
for cmd in llm-server models stats agent run; do
  if "$MIMO_BIN" "$cmd" --help >/dev/null 2>&1; then
    echo "  $cmd: ok"
  else
    echo "  $cmd: unavailable"
  fi
done

echo
echo "llm-server tokens (this directory):"
"$MIMO_BIN" llm-server list 2>&1 || true
