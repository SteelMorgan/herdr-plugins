# Issue draft: App UI freezes while a managed machine is unreachable

**Suggested title:** Chat input / UI freezes in ~10s chunks when any managed machine is unreachable — machine probes appear synchronous and serialized

## Environment

- Herdr App 0.1.15, herdr-core 0.1.21, Windows 11 (local core).
- Managed machines over SSH: `dev` (direct SSH to a VM), `1c` (SSH with a docker-stdio ProxyCommand).
- No third-party mods docked at the time of observation (App started before any mod was installed), so the behavior is core/App-side, not plugin-side.

## Symptom

When one managed machine becomes unreachable (VM freeze, network loss), the chat input line and parts of the UI intermittently freeze for up to ~10 seconds at a time, repeating for as long as the machine stays down. Even with all machines healthy, occasional shorter hitches occur. Losing one machine should degrade only that machine's status row — it should not stall the whole interface.

## Evidence

- `hc machine ls --json` reports the machine `state: "unreachable"` with `why: "cannot reach <target>: ssh: connect to host <ip> port 22: Connection timed out"`.
- The core binary spawns the system `ssh` with hard-coded options `-o BatchMode=yes -o ConnectTimeout=10` (and `ServerAliveInterval=15 ServerAliveCountMax=4` for sessions). Each probe of a black-holed host therefore occupies a worker for the full 10s TCP timeout.
- The binary contains the string `machines serialize`, suggesting machine operations run through a serialized queue. GUI/API requests appear to wait behind in-flight probes, which matches the observed ~10s input stalls aligned with probe cycles.
- A `machine_timeout` key exists in the core config (`config.rs`, `%APPDATA%\herdr-core\config.toml` on Windows) but is undocumented; it is unclear whether it caps probes and whether it is read at runtime or only at startup.
- Third-party plugin polling was reduced/backed off (30s healthy / 300s degraded) as a mitigation; this lowers queue pressure but does not remove the core's own probes, and the freezes persist while a machine is down.

## Reproduction

1. Add an SSH machine (`hc machine add test user@host`).
2. Make it unreachable without removing it (stop the VM, or black-hole the IP so TCP SYNs time out rather than get RST).
3. Keep typing in the App chat input and interact with the UI.
4. Observe periodic freezes of up to ~10s repeating while the machine is unreachable; they stop once it reconnects or is removed.

## Expected behavior

- Machine reachability probes run asynchronously with short timeouts and cached state; the UI never blocks on machine I/O.
- GUI/API requests are decoupled from the probe queue (a slow probe must not head-of-line-block interactive requests).
- Unreachable machines are retried with backoff instead of a fixed frequent cycle.

## Requests

1. Make the machine probe pipeline async and non-blocking for the UI thread / API handlers.
2. Document `machine_timeout` (semantics, default, runtime vs startup) and honor it for probes; consider a separate short probe timeout.
3. Exponential backoff for unreachable machines (e.g. 5s → 30s → 300s), reset on reconnect.
4. Keep serving cached machine state to the UI while a probe is in flight.

## Notes for maintainers

Happy to provide core logs with `HERDR_LOG=debug` and a packet capture of the probe cycle if useful. The serialized-queue observation is inferred from binary strings and timing, not from source access.
