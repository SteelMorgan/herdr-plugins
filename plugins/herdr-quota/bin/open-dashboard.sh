#!/bin/sh
set -eu

# Plugin actions inherit the pane that invoked them. Keeping it explicit avoids
# accidentally splitting whichever pane another Herdr client happens to focus.
args="--plugin herdr.quota --entrypoint dashboard --placement split --direction right --focus"
if [ -n "${HERDR_PANE_ID:-}" ]; then
  # shellcheck disable=SC2086
  result=$(herdr plugin pane open $args --target-pane "$HERDR_PANE_ID")
else
  # shellcheck disable=SC2086
  result=$(herdr plugin pane open $args)
fi
printf '%s\n' "$result"
