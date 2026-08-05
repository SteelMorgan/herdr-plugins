# Herdr Resources

Live terminal dashboard for dev-containers configured in Herdr Mirror.

It discovers hosts from `~/.config/herdr-mirror/hosts.toml`, resolves Docker
dev-containers, and displays container availability, local Herdr/Mirror state,
CPU, memory and root-filesystem usage. The layout adapts to narrow panes.

## Requirements

- Linux host with Docker CLI access
- Herdr Mirror configured through `hosts.toml`
- Python 3.11 or newer

No credentials are read or stored by this plugin.
