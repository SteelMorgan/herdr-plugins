# ONEC SSH reverse-tunnel control (guard + watchdog), hardened 2026-11-15

Канонический репо инфраструктуры: `gbig-holdings/dev-proxmox`
(`proxmox/bootstrap/ONEC-SSH-TUNNEL-CONTROL.md`). Локального checkout с
исходниками здесь не оказалось — установленные копии правились напрямую;
этот каталог хранит проверенные исходники и тесты для синхронизации в
dev-proxmox.

## Архитектура

- **WSL** `onec-v8sm-watchdog` (демон `/usr/local/libexec/onec-v8sm-watchdog.py`,
  запуск из `/usr/local/bin/herdr-boot.sh`): каждые 20 с следит за sidecar
  `onec-v8sm-tunnel` в docker (сеть контейнера `1c-ai-sandbox`), перезапускает
  его при exited/rebind и независимо вызывает серверный guard через public jump.
- **VM110** `/usr/local/sbin/onec-ssh-tunnel-guard` (root): наблюдает единственный
  listener `127.0.0.1:4000`, принадлежащий `onec-v8sm-tunnel`; убивает только
  доказанно осиротевшие sshd-сессии (pidfd + повторные подтверждения + cooldown).
  Scoped sshd-политика: `sshd_config.d/100-onec-v8sm-tunnel.conf`
  (ClientAlive 15×3, PermitListen 127.0.0.1:4000, ForceCommand /bin/false).

## Инцидент 2026-11-15 и доработка

Контейнер туннеля циклился (restarts=123): старый ssh-сеанс переживал рестарт
контейнера и держал :4000 на VM110; новый клиент получал
`remote port forwarding failed` и выходил по ExitOnForwardFailure.
Watchdog был жив и вызывал guard, но вердикт `healthy_or_unproven`:
старый критерий stale требовал `send_queue>0 + backoff>=3` (сервер достучивается
до мёртвого клиента), а «тихий» орфан с пустой очередью под него не попадал.

Доработка (сохраняет консервативную модель):
- watchdog передаёт `--container-started <StartedAt ISO>` текущего контейнера;
- guard считает сессию осиротевшей, если она **старше старта контейнера** (−5 с
  на skew) и простаивает `lastrcv/lastack >= 180 с`. Здоровый туннель отвечает
  на scoped keepalive 15 с, поэтому lastrcv не достигает порога живым;
- все прежние защиты сохранены: 3 подтверждения / 40 с, cooldown 120 с,
  pidfd-identity recheck перед SIGTERM.

## Проверка

- Тесты: `python3 -m unittest test_guard_stale` (7/7 на VM110, 2026-11-15).
- Ручной inspect: `sudo onec-ssh-tunnel-guard inspect` /
  `check --container-started <ISO>`.
- Статус watchdog: `onec-v8sm-watchdog status`; лог
  `~/.local/state/onec-v8sm-watchdog/watchdog.log`.

## Деплой

- guard: `sudo tee /usr/local/sbin/onec-ssh-tunnel-guard && chmod 755` (VM110);
- watchdog: `sudo tee /usr/local/libexec/onec-v8sm-watchdog.py` (WSL) +
  `onec-v8sm-watchdog restart`;
- клиентская чистка старого ssh при старте контейнера (dev-хост) — отдельная
  задача, доступ к 192.168.250.2 требуется.
