#!/bin/bash
# VM200 had NO swap: kswapd thrashed the file cache (refault reads ~260MB/s).
# Give memory pressure a cheap outlet: zram swap (compressed RAM, no disk),
# plus hard io caps on the heavy slice so any disk IO (incl. future swap) is trimmed.
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
printf 'vm.swappiness = 100\n' > /etc/sysctl.d/90-gbig-swap.conf
sysctl -w vm.swappiness=100 >/dev/null
systemctl daemon-reload
systemctl start systemd-zram-setup@zram0.service
systemctl set-property user.slice "IOReadBandwidthMax=/dev/sda 100M" "IOWriteBandwidthMax=/dev/sda 50M"
echo ---swaps---; cat /proc/swaps
echo ---swappiness---; cat /proc/sys/vm/swappiness
echo ---user-slice-io---; cg=$(systemctl show user.slice -p ControlGroup --value); cat "/sys/fs/cgroup$cg/io.max"
echo SWAP_TRIM_OK
