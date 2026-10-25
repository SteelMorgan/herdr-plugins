#!/bin/sh
set -eu
DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
export NODE_PATH="${NODE_PATH:-/home/steel/.local/node-v24.19.0-linux-x64/lib/node_modules}${NODE_PATH:+:$NODE_PATH}"
exec node "$DIR/capture-mimo-cookies.mjs" "$@"
