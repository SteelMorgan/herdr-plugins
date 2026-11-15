#!/bin/bash
systemctl set-property gbig-mgmt.slice IOWeight=1000 CPUWeight=1000
systemctl set-property user.slice IOWeight=50
cg=$(systemctl show gbig-mgmt.slice -p ControlGroup --value)
if [ -f "/sys/fs/cgroup$cg/io.weight" ]; then
  echo "io.weight: $(cat /sys/fs/cgroup$cg/io.weight)"
else
  echo "WARN: io.weight still missing at $cg"
fi
for p in $(pgrep -f export_digest) $(pgrep -f gbig-radar-embed-server); do
  ionice -c 3 -p "$p" 2>/dev/null
  renice -n 10 -p "$p" >/dev/null 2>&1
done
echo IO_FIX_DONE
