#!/bin/bash
# Cap Radar batch units so producers/embed can never starve the management plane.
set -euo pipefail
for u in gbig-radar-producer-collect@.service gbig-radar-producer-conveyor@.service \
         gbig-radar-producer-inbox@.service gbig-radar-producer-extremes@.service \
         gbig-radar-ideas-prepare@.service gbig-radar-informing@.service \
         gbig-radar-prepublication@.service gbig-radar-maintenance.service \
         gbig-radar-feed-retention.service; do
  systemctl cat "$u" >/dev/null 2>&1 || continue
  mkdir -p "/etc/systemd/system/$u.d"
  cat > "/etc/systemd/system/$u.d/60-resource-cap.conf" <<'EOF'
[Service]
CPUWeight=50
IOWeight=50
Nice=10
IOSchedulingClass=idle
IOSchedulingPriority=7
MemoryHigh=2G
EOF
done
# embed keeps its 4G MemoryHigh (holds ~3.2G RSS); only scheduling/IO caps here
mkdir -p /etc/systemd/system/gbig-radar-embed.service.d
cat > /etc/systemd/system/gbig-radar-embed.service.d/60-resource-cap.conf <<'EOF'
[Service]
CPUWeight=50
IOWeight=50
Nice=10
IOSchedulingClass=idle
IOSchedulingPriority=7
EOF
systemctl daemon-reload
systemctl show gbig-radar-producer-conveyor@.service -p CPUWeight -p Nice -p MemoryHigh -p IOSchedulingClass
systemctl show gbig-radar-embed.service -p MemoryHigh -p Nice
echo RADAR_CAP_OK
