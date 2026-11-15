#!/bin/bash
# VM-side management protection. Args: extra units to protect.
# Heavy side: user.slice (agents/builds) gets lower relative weights.
set -euo pipefail
mkdir -p /etc/systemd/system/gbig-mgmt.slice.d
printf '[Slice]\nCPUWeight=1000\nIOWeight=1000\nMemoryMin=268435456\n' > /etc/systemd/system/gbig-mgmt.slice.d/50-priority.conf
for u in ssh.service sshd.service qemu-guest-agent.service "$@"; do
  systemctl cat "$u" >/dev/null 2>&1 || continue
  mkdir -p "/etc/systemd/system/$u.d"
  printf '[Service]\nSlice=gbig-mgmt.slice\nNice=-5\n' > "/etc/systemd/system/$u.d/50-mgmt.conf"
done
systemctl daemon-reload
# Relative weights only bite under contention; idle performance unchanged.
systemctl set-property user.slice CPUWeight=50 IOWeight=50
systemd-analyze verify ssh.service 2>/dev/null || systemd-analyze verify sshd.service 2>/dev/null || true
echo HARDEN_VM_OK
