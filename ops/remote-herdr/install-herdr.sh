#!/usr/bin/env bash
# Run as steel on dev-agents; installs binary only, no sessions or agents.
set -euo pipefail
herdr_version=0.9.0
herdr_sha=4fa1a01158dd8043da92d31b270780b0dcc10603038d9b61cac4d81ab63fb71f
herdr_dir="/home/steel/herdr-${herdr_version}"
mkdir -p "$herdr_dir"
curl -fsSL --connect-timeout 10 --max-time 120 \
  "https://github.com/herdrdev/herdr/releases/download/v${herdr_version}/herdr-linux-x86_64" \
  -o "$herdr_dir/herdr"
printf '%s  %s\n' "$herdr_sha" "$herdr_dir/herdr" | sha256sum -c -
mkdir -p /home/steel/.local/bin
install -m 0755 "$herdr_dir/herdr" /home/steel/.local/bin/herdr
/home/steel/.local/bin/herdr --version
