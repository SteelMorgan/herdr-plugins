#!/bin/sh
# Mint a scoped LLM-server token for this project.
set -eu
. "$(dirname "$0")/common.sh"
run_mimo llm-server issue
