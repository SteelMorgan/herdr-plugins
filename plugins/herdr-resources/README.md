# Herdr Resources

Live terminal dashboard for dev-containers configured in Herdr Mirror.

It discovers hosts from `~/.config/herdr-mirror/hosts.toml`, resolves Docker
dev-containers, and displays container availability, local Herdr/Mirror state,
CPU, memory and root-filesystem usage. SSH hosts are sampled with one short
remote probe every 15 seconds; the probe reads `/proc` and `df` directly and
does not install or leave a monitoring process on the server. The layout adapts
to narrow panes.

## Requirements

- Linux host with Docker CLI access
- Herdr Mirror configured through `hosts.toml`
- Python 3.11 or newer

No credentials are read or stored by this plugin.

Disabled mirror hosts are marked DISABLED and are never probed or started.
The startup hook reuses uniquely labelled panels in the dashboards workspace,
retries API/session readiness for up to 60 seconds, and only types into an idle
shell with the expected cwd. Busy panels remain untouched. Concurrent launches
are locked; repeated startup hooks do not duplicate collectors. Startup events:
~/.local/state/herdr-dashboards/startup.log. A manually closed dashboard stays
closed until the hook is explicitly run or Herdr restarts.
