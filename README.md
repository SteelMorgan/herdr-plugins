# Herdr plugins and workstation integrations

Maintained source for the local Herdr setup. Each plugin directory contains its
manifest, source and usage notes. Link only the plugins you need; no blanket
installer or service activation runs on checkout.

| Component | Purpose |
| --- | --- |
| [herdr-quota](plugins/herdr-quota/) | Codex/Claude/MiMo and other provider usage, secure cookie refresh and normalized usage export |
| [herdr-resources](plugins/herdr-resources/) | Local Docker and SSH mirror resources; bounded dashboard startup recovery |
| [herdr-pve-health](plugins/herdr-pve-health/) | Low-frequency SSH server/SMART/VM/application/backup health |
| [herdr-mimo](plugins/herdr-mimo/) | MiMo CLI panes and actions |
| [herdr-capacity-guard](plugins/herdr-capacity-guard/) | Codex capacity-error retry watcher |
| [Extensions overlay](overlays/herdr-extensions/) | Local installed shell/manifest adjustments against a pinned upstream revision |
| [Collie overlay](overlays/collie/) | Narrow Codex goal-footer recognition and regression fixture |
| [Host operations](ops/) | Mirror watchdog, server crash logging, shared D-Bus bus, formatter wrapper, remote Herdr service and keybindings |

Credentials, browser profiles/cookies, SSH identities, live health snapshots,
private mirror inventories and recovery backups are deliberately kept outside
Git. Operator configuration belongs in Herdr plugin config directories. Some
operational scripts preserve this workstation's paths/user and require review
before installation on another host. Upstream Herdr, Mirror, Collie and extensions
binaries remain independently installed; this is not a vendor copy of them.

The `dashboards` workspace uses two upper panels (resources left, usage right)
and a full-width lower server-health panel. Opening it resizes terminal viewports;
collectors keep running while another workspace is focused. The startup restorer
reuses only uniquely identified panes in that workspace, preserves focus, and
never injects commands into an occupied agent/editor pane.

## Validation

```sh
python3 -m unittest discover -s plugins/herdr-pve-health/tests -v
python3 -m unittest discover -s plugins/herdr-capacity-guard/tests -v
```

Dashboard health probes are read-only; usage publication and cookie refresh are
separate explicitly configured features. Read each component README before use.
