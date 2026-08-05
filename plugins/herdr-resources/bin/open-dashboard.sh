#!/bin/sh
set -eu

set -- --plugin herdr.resources --entrypoint dashboard --placement split --direction right --focus
if [ -n "${HERDR_PANE_ID:-}" ]; then
  herdr plugin pane open "$@" --target-pane "$HERDR_PANE_ID"
else
  herdr plugin pane open "$@"
fi
