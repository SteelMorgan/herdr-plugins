# herdr-pve-health (Herdr App)

App instance of `../../plugins/herdr-pve-health`. All collection intervals,
cache invalidation, metrics math, SMART handling, the Russian overview
cards (`presentation.py`) and the remote `probe.py` are unchanged — see the
TUI README. `probe.py` executes only on the PVE host over SSH.

Adapted surface:
- config default → `<appHome>/plugins/config/herdr.pve-health/config.json`
  (`config.example.json`; `windows_backup_file` accepts native Windows paths
  like `D:/GBIG-Backups/Proxmox/last-success.json`);
- state/cache → `~/.local/state/herdr-app/herdr.pve-health/`;
- keyboard → `lib/console_input.py` (Windows: `d` details, `r` light refresh,
  `q` quit);
- needs an SSH client (`ssh.exe` on Windows) and the usual `~/.ssh/config`.

Actions: open, diagnose (one collect). The shared startup restorer lives in
herdr-resources, exactly as in the TUI instance.