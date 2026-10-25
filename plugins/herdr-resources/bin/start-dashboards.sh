#!/bin/sh
# Startup hook retries while the API/session is becoming ready. Never changes focus.
set -eu
exec /usr/bin/python3 /home/steel/projects/herdr-plugins/plugins/herdr-resources/bin/restore-dashboards.py
