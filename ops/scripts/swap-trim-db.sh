#!/bin/bash
# DB/1C production VM: zram as a safety net only. Conservative swappiness so
# hot anonymous pages (Postgres/1C) stay in RAM; cache is reclaimed first.
# No io caps here - production workload must not be throttled.
set -euo pipefail
if ! dpkg -s systemd-zram-generator >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y systemd-zram-generator >/dev/null 2>&1
fi
cat > /etc/systemd/zram-generator.conf <<'EOF'
[zram0]
zram-size = 4096
compression-algorithm = zstd
swap-priority = 100
EOF
printf 'vm.swappiness = 10\n' > /etc/sysctl.d/90-gbig-swap.conf
sysctl -w vm.swappiness=10 >/dev/null
systemctl daemon-reload
systemctl start systemd-zram-setup@zram0.service
echo ---swaps---; cat /proc/swaps | tail -1
echo ---swappiness---; cat /proc/sys/vm/swappiness
echo SWAP_TRIM_DB_OK
