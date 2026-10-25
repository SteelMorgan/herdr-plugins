#!/bin/sh
set -eu
stage=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/herdr-mirror-boot-watchdog.sh
watchdog=/usr/local/bin/herdr-mirror-boot-watchdog
stamp=$(/usr/bin/date +%Y%m%d-%H%M%S)
sudo -n /bin/cp -p "$watchdog" "$watchdog.before-supervisor-lock-fix-$stamp"
sudo -n /usr/bin/install -o nobody -g nogroup -m 755 "$stage" "$watchdog"
sudo -n /bin/sh -c '/usr/bin/setsid /usr/bin/nohup /usr/sbin/runuser -u steel -- /usr/local/bin/herdr-mirror-boot-watchdog >>/var/log/herdr-mirror-boot.log 2>&1 </dev/null &'
printf '%s installed watchdog with isolated supervisor lock and launched monitor\n' \
    "$(/usr/bin/date '+%Y-%m-%dT%H:%M:%S%z')"
