#!/bin/sh
# List LLM-server tokens for this directory.
set -eu
. "$(dirname "$0")/common.sh"
run_mimo llm-server list
