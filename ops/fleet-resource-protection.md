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
5c. **VM200, swap/zram**: swap отсутствовал вовсе — шторм был refault-чтением
   файлового кэша. Добавлен zram0 (4G, zstd, priority 100,
   `systemd-zram-generator`, `/etc/systemd/zram-generator.conf`),
   `vm.swappiness=100` (`/etc/sysctl.d/90-gbig-swap.conf`) — давление памяти
   уходит в сжатую RAM, а не на диск. `user.slice` получил жёсткий
   `io.max` 100M read / 50M write (любой дисковый IO тяжёлых cgroup, включая
   возможный будущий disk-swap, подрезан). Скрипт: `ops/scripts/swap-trim.sh`.
5b. **VM200, Radar-батчи**: drop-in `60-resource-cap.conf` на шаблонах
   `gbig-radar-producer-{collect,conveyor,inbox,extremes}@`,
   `gbig-radar-{ideas-prepare,informing,prepublication}@`,
   `gbig-radar-{maintenance,feed-retention}`: CPUWeight=50, IOWeight=50,
   Nice=10, IOSchedulingClass=idle, MemoryHigh=2G. На `gbig-radar-embed` —
   те же scheduling/IO-капы БЕЗ MemoryHigh (сохранён 4G, embed держит ~3.2G).
6. **Windows/core**: `%APPDATA%\herdr-core\config.toml` — `machine_timeout=3`,
   `notes=false` (без `<hc from>`-обвязки и автонот). Плагины: guard скан 30 с /
   300 с при недоступной машине; отправка сообщений без terminal-identity.

## RAM-картина (15.11 ~16:40)

- **PVE 64G**: used ~60G, avail ~4G, swap 2.6/4.7G. RSS VM: 100≈1G, 110≈16.3G,
  111≈0.3G, 200≈20.5G, 201≈16G, 300≈3.9G. Хост в overcommit — главный
  дефицит контура; при росте VM хост свопит. Рекомендация: пересмотреть
  аллокации/ballooning (VM200 после ребута потребляет ~4G из 20G).
- **VM200 20G**: после ребута used ~4.3G (агенты не резюмлены), zram0 4G готов;
  Radar embed ~2.8G. При возврате агентов (+7–8G) упрётся в MemoryHigh 16G —
  лимиты сработают штатно.
- **VM201 16G**: used ~11.8G, avail ~4.2G; база k3s 2.3G + java/redpanda/
  containerd/node/minio ≈ 2G; swap не было — добавлен zram0 4G + swappiness 100
  + user.slice io.max 100M/50M (тем же `swap-trim.sh`).
- **VM110 16G (1С+Postgres, продакшен)**: давления нет (avail ~12G, postgres
  в page cache), но swap не было — добавлен zram0 4G как страховка от пиков
  (регламенты/work_mem). **swappiness=10** (консервативно: горячие анонимные
  страницы БД остаются в RAM), без io-капов — продакшен не дросселируем.
  Скрипт: `ops/scripts/swap-trim-db.sh`. Нюанс: после ручной установки
  `systemctl start dev-zram0.swap` (генератор создаёт юнит, но среди сессии
  swap-юнит надо активировать); при загрузке подхватывается через
  `swap.target.wants` (проверено на 110/200/201).

5d. **VM200, /tmp tmpfs-cap (инцидент 15.11 ~20:00)**: /tmp смонтирован tmpfs;
   агентские сборки залили туда 9G неосвобождаемой shmem → RAM исчерпана,
   zram полон, reclaim-шторм, SSH в ступоре (IO-защита тут не помогает — диск
   не при чём). Лечение: неиспользуемые build-каталоги перенесены в
   `/var/tmp/ram-relief-20261115/` (данные сохранены), `/tmp` ограничен 4G
   (remount + drop-in `tmp.mount.d/50-size-cap.conf`, персистентно). Правило:
   сборки — в worktree, не в /tmp; cap не даёт tmpfs съесть RAM повторно.

## Правило после сетевых изменений (инцидент 15.11 вечер)

Перезапуск интерфейсов/туннелей ломает зависимые сервисы: dnsmasq
(gbig-internal-dns, bind-dynamic) потерял биндинг 10.77.90.1 после рестарта
wg0 → прод-клиенты получили «хост неизвестен». Исправлено рестартом сервиса
+ `PartOf=wg-quick@wg0.service`. После любых сетевых изменений проверять:
DNS-листнеры (`ss -lunp sport = :53`), туннели (:4000), watchdog-статусы,
маршруты и allowed-ips пиров (без перекрытий между пирами сервера).

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

## Known issues / статус на 15.11 16:35

- **Причина шторма установлена**: OOM + reclaim. В предыдущей загрузке ядро
  убило DSH-агента (UID 1001, oom_score_adj 200, journalctl -b -1, 14.11 21:20);
  Radar-продюсеры (таймеры conveyor/inbox/extremes ~каждые 2 мин) + embed +
  агенты/сборки исчерпали RAM 20G → kswapd-шторм чтения ~260 МБ/с → ступор
  sshd/QGA. 15.11 ~16:27 VM200 перезагружена через QMP system_reset (явное
  согласие владельца); после загрузки ключ телефона добавлен, io.weight=1000
  применился, капы Radar установлены (п. 5b).
- На VM200 ранее `io.weight` не появлялся (io-контроллер не включался на
  slice); `set-property IOWeight` включает контроллер корректно — после
  ребута подтверждено (io.weight 1000).
- VM201 era-dev: IO pressure ~30% от node-сборок — защита применена, при
  повторении смотреть сборки/агентов.
- VM300 mail: доступ по ключу pve-agent починен владельцем 15.11, защита применена.

## Диск VM200: карта и очистка 16.11

Инструмент: `ops/scripts/disk-map.py` (на VM200 `~/.local/bin/disk-map`,
запуск `sudo disk-map`): полная карта с ionice idle/nice 19, JSON+TXT в
`~/.local/state/disk-map/`, повторный запуск даёт diff с предыдущим.
Ничего не удаляет.

Карта 16.11: 120G/148G (85%). Рост дали билд-каталоги агентов от 15.11
(~26G) и radar.db (330M→2.8G). По явному согласию владельца удалено:
- 8 путей завершённых билдов 15.11 (6 в /home/hermes: b6-delta-wHQFQ9cn,
  hr-reader/identity-build, hr-replay-merge/build/fix; /var/tmp/ram-relief-20261115
  целиком; /tmp/b6-build-LI2S6VPx). Проверено: .git внутри нет, процессов
  с cwd/open files нет, образы этих билдов уже в podman;
- dangling-образы podman (`podman image prune -f`, ~400 записей включая
  промежуточные слои); тегированные ревизии билдов владелец оставил;
- 378 каталогов /root/gbig-* (21.7G, установочные/квалификационные остатки
  30.10–06.11, ссылок из systemd/cron/live-кода ноль).

Сохранено: pinned recovery-образы gbig-local-speech 640e31e8f88c и
gbig-local-media 8c1e2ad0fa9e (сверены с IMAGE-LOCK), базы/registry-образы,
worktrees, hermes/work, CLI, .hermes, Office-образы, живые данные Radar
(/var/lib/gbig-radar, /opt/gbig-radar) — зона Radar не тронута.

Итог: 68G/148G (48%), свободно 74G; /tmp 2.9G→298M. Освобождено ~52GiB.
Паттерн повторяется: агенты строят в /tmp и оставляют source.tar+tasks
после завершения — при росте диска сначала смотреть свежие билд-каталоги
и dangling podman.
