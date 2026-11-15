# Защита management-плоскости контура (2026-11-15)

Цель: тяжёлые задачи (Radar-экспорты, 1С, сборки, coding-агенты) не должны
подвешивать SSH, herdr-core и интерфейс Herdr App. Повод: VM200 дважды уходила
в ступор (Radar `export_digest` читал ~260 МБ/с + swap-thrash), SSH/QGA молчали.

## Механизмы

1. **Management-slice** `gbig-mgmt.slice`: `CPUWeight=1000`, `IOWeight=1000`,
   `MemoryMin=256M`. Члены: `ssh.service` (+`Nice=-5`), `qemu-guest-agent`,
   на VM200 — `gbig-hermes-gateway/office`, на PVE — `pve-cluster/pvedaemon/
   pvestatd/pveproxy`. Drop-in’ы: `/etc/systemd/system/gbig-mgmt.slice.d/
   50-priority.conf` и `<unit>.d/50-mgmt.conf`.
2. **Приоритеты при конкуренции**: iocost включён на дисках (`io.cost.qos
   enable=1`), `io.weight` management 800–1000 против 50 у тяжёлых;
   `user.slice` CPU/IOWeight=50. Веса работают только под нагрузкой.
3. **Жёсткие потолки IO** (гарантия полосы системе):
   - PVE: скоупы `200.scope`/`110.scope` — `io.max` 200M read / 100M write на
   каждый диск (drop-in `60-io-caps.conf`);
   - VM110: контейнер `onec-server` — cpu-shares 512, blkio-weight 500;
   - VM200: `gbig-radar-embed.service` — IORead/WriteBandwidthMax 100M/50M,
   `MemoryHigh=4G`; `user.slice MemoryHigh=16G` (НЕ ниже: 12G уже вызывал
   reclaim-шторм с load 560).
4. **Персистентность**: `/usr/local/sbin/gbig-io-protect` +
   `gbig-io-protect.service` (enabled) на PVE, VM100/110/111/200/201/300 —
   применяет iocost/веса при загрузке.
5. **VM200, запуск агентов**: `/usr/local/sbin/gbig-agent-route` оборачивает
   CLI в `ionice -c2 -n7 nice -n10`; `DISPLAY=:99` + `xvfb.service` (Xvfb :99)
   для буфера обмена (xclip/xsel установлены; на 1c стек уже был).
6. **Windows/core**: `%APPDATA%\herdr-core\config.toml` — `machine_timeout=3`,
   `notes=false` (без `<hc from>`-обвязки и автонот). Плагины: guard скан 30 с /
   300 с при недоступной машине; отправка сообщений без terminal-identity.

## Проверка (по хостам)

```bash
systemctl show gbig-mgmt.slice -p CPUWeight -p IOWeight -p MemoryMin
systemctl show ssh.service -p Slice -p Nice
systemctl is-enabled gbig-io-protect.service
cg=$(systemctl show gbig-mgmt.slice -p ControlGroup --value); cat /sys/fs/cgroup$cg/io.weight
# PVE:
cat /sys/fs/cgroup/qemu.slice/200.scope/io.max; systemctl show 200.scope -p CPUWeight
# VM110:
docker inspect onec-server --format '{{.HostConfig.CpuShares}} {{.HostConfig.BlkioWeight}}'
# VM200:
systemctl show gbig-radar-embed.service -p IOReadBandwidthMax -p MemoryHigh
systemctl show user.slice -p MemoryHigh; systemctl is-active xvfb.service
```

Ожидаемо: mgmt 1000/1000/256M, ssh Slice=gbig-mgmt.slice Nice=-5, io.weight
800–1000, на PVE io.max rbps=209715200 wbps=104857600, на 110 shares=512
blkio=500, на 200 radar 100M/50M + MemoryHigh 4G, user.slice 16G.

## Откат

- drop-in’ы `50-*.conf`/`60-io-caps.conf` удалить + `daemon-reload`;
- `systemctl set-property <unit> CPUWeight=100 IOWeight=100` (снимает и io.max);
- `systemctl disable --now gbig-io-protect.service xvfb.service`;
- VM110: `docker update --cpu-shares 1024 --blkio-weight 0 onec-server`;
- Windows: убрать строки из config.toml (перезапуск core).

## Known issues / статус на 15.11 16:00

- VM200 в момент написания заморожена (ожидает восстановления без ребута по
  решению владельца); по recovery применяются ключ телефона + IO-веса
  (`io-fix.sh`), затем разбор причины шторма и донастройка.
- На VM200 ранее `io.weight` не появлялся (io-контроллер не включался на
  slice) — проверять после восстановления; `set-property IOWeight` включает
  контроллер корректно (подтверждено на VM300).
- VM201 era-dev: IO pressure ~30% от node-сборок — защита применена, при
  повторении смотреть сборки/агентов.
- VM300 mail: доступ по ключу pve-agent починен владельцем 15.11, защита применена.
