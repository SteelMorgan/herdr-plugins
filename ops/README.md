# Workstation and remote Herdr support

These are source copies of the existing operational setup, not an instruction to
reconfigure a running workstation. Review paths and usernames before installing.
No installation or restart was performed while archiving these scripts.

* `herdr-mirror-boot-watchdog.sh`: persistent central Mirror supervisor, separate
  lock from daemon descriptors, readiness retries, sticky pause preservation,
  five-minute heartbeat and bounded failure diagnostics (memory/OOM/process/log).
  Current installation is `/usr/local/bin/herdr-mirror-boot-watchdog`, invoked as
  `steel`; output goes to `/var/log/herdr-mirror-boot.log`.
* `herdr-mirror-watchdog-install.sh`: backs up the existing supervisor and installs
  the adjacent source. Intended for the original host with its existing log and
  startup hook; it is not a first-install bootstrap and must be invoked manually.
* `herdr-server-start`: starts the central server only if absent, with stderr and
  Rust backtraces retained and bounded log rotation. Do not use it to replace or
  stop a live server.
* `session-bus.sh`: shared, lock-protected WSL D-Bus session for Docker Desktop
  clients, avoiding a leaked daemon per probe. Source from the user's shell setup.
* `herdr-fmt`: project-first formatter resolution for the editor plugin.
* `keybindings.json`: only local key commands from Herdr configuration; not a
  complete configuration. Translate to `[[keys.command]]` TOML when restoring and
  check collisions with the installed Herdr version before reloading.
* `upstream-versions.json`: provenance for clean upstream source checkouts. Keep
  upstream installers for Herdr/extensions rather than copying installed binaries.
* `remote-herdr/`: original VM200 binary installer and VPN-namespace systemd unit.
  It depends on existing VPN/network/proxy services; it does not create them or
  contain their keys. Install the unit manually only after those prerequisites.

## Mirror restoration invariants

Keep an intentionally stopped sandbox stopped while retaining its enabled mirror.
Resolve actual workspace/tab/pane IDs and current host mapping from the CLI.
A daemon PID or workspace count does not prove a visible bridge: compare each
pane's visible output and foreground process. Repair a missing streamer in its
exact mapped pane, without replacing the remote agent. Preserve the user's focus.

After a host reinstall, update pinned host keys through the operator's verified
SSH setup. Never publish the live SSH key/config inventory here. For VM200,
SSH transport is outside the VPN namespace; Herdr terminals run inside it under
`hermes`. Keep plain API routes such as MiMo/DeepSeek distinct from VPN-only CLIs.

## Dashboard persistence

All three monitor manifests call the shared resources startup script. It uses
flock and a 60-second bounded readiness retry. Panel labels and cwd must match,
and foreground process checks distinguish a shell from a live TUI/agent.
Re-running the restorer was verified against the live processes without changing
PIDs or creating duplicates. A full Windows/WSL reboot was not used for this check.

The server collector keeps local last-good samples and displays their age. A
Windows backup copy timestamp does not make older VM snapshots fresh. Disabled
mirror entries are not probed; stopped containers are never started by monitors.
