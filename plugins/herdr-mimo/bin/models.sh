#!/bin/sh
# List available models.
set -eu
. "$(dirname "$0")/common.sh"
run_mimo models
