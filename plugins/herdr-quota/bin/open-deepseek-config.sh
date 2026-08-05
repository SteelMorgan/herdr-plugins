#!/bin/sh
set -eu

args="--plugin herdr.quota --entrypoint deepseek-config --placement popup --width 68 --height 15 --focus"
# shellcheck disable=SC2086
exec herdr plugin pane open $args
