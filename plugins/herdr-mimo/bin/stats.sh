#!/bin/sh
# Token usage / cost stats.
set -eu
. "$(dirname "$0")/common.sh"
run_mimo stats
