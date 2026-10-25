# Dev server health (`herdr.pve-health`)

Read-only SSH dashboard for a Proxmox host, SMART, filesystems, VMs, selected
services, WireGuard, application connectivity and backup freshness. The collector
runs on the workstation; Python probes are sent over SSH stdin and leave no
installed monitoring agent on the server. Requires Python 3, SSH key access,
noninteractive sudo for the probes, smartmontools and standard Proxmox utilities.

Copy `config.example.json` to the plugin's config directory as `config.json` and
adjust the SSH aliases and optional application/Windows backup checks. Credentials
stay in SSH/config directories outside this repository. The example service names
and backup marker paths describe this deployment and should be adapted elsewhere.

| Group | Interval |
| --- | --- |
| CPU, RAM, swap, PSI, temperature, disk and physical NIC counters | 30 seconds |
| Filesystem space, inode use, read-only flag, LVM thin pools | 60 seconds |
| VM state, host services and bounded kernel warning scan | 120 seconds |
| Backup markers/jobs, application connectivity and auxiliary VM checks | 5 minutes |
| SMART health, vendor attributes and identity | 15 minutes |

Only due groups are collected. Results persist locally in
`~/.local/state/herdr-pve-health/cache.json`, invalidated when configuration changes.
`r` refreshes light metrics without invalidating cached SMART. Failed groups retry
within 60 seconds; stale/error data is marked rather than presented as healthy.
The terminal redraws locally once per second without additional remote polling.

On the 12-thread deployment host, a full collection used about 0.95 CPU seconds
and 1.47 seconds SSH wall time; the VM inventory accounted for most of that cost.
The basic `/proc` sample took about 2.4 ms. SSH authentication overhead is additional
to the measured Python/child CPU time. No SMART self-test, disk benchmark, full
backup checksum reread or database query is performed. SATA standby is respected.

SMART raw attributes are vendor-specific: factory bad-block counts are not
relabeled as pending/reallocated sectors. Unsupported fields show `--`. VM memory
is host QEMU RSS, which may exceed configured guest RAM. Application checks prove
connectivity/status only, not database integrity. Kernel counts cover at most 80
warnings from the last 30 minutes, not the complete historical journal. Backup
age reflects VM archive timestamps separately from export and Windows copy time.
WireGuard probes return counts/times only; no private-key dump is requested.

```sh
herdr plugin link /absolute/path/plugins/herdr-pve-health --enabled
herdr plugin pane open --plugin herdr.pve-health --entrypoint dashboard \
  --placement split --direction down --target-pane <pane-id> --no-focus
python3 -m unittest discover -s plugins/herdr-pve-health/tests -v
```

The established `dashboards` workspace has resources and usage side by side above
this full-width bottom panel. The shared resources-plugin startup restorer resumes
existing designated shell panes without changing focus or restarting live TUIs.
