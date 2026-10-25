# Herdr Codex Capacity Guard

This local Herdr plugin watches recognized Codex panes for the exact message
`Selected model is at capacity. Please try a different model.` The first
occurrence sends `Продолжить` immediately. If the same pane shows a new capacity
error after that prompt, the plugin marks it `модель перегружена` in red and
starts a ten-minute timer. When the timer expires, it sends `Продолжить` again.
If capacity is still unavailable after that retry, the same ten-minute cycle
repeats. The red label lasts only for the active wait and is cleared after a
retry is submitted or Codex finishes.

For mirrored panes, Herdr may report `agent_not_ready` because the foreground
process on the central side is the mirror client. If Herdr still recognizes the
target pane as Codex, the plugin sends the text and Enter through that pane's
terminal input. A failed send leaves the pane marked for attention and raises a
notification. The plugin stores each pane's retry phase and deadline so a
Herdr restart preserves an active wait.

The watcher only targets panes Herdr identifies as `codex`. It uses the Herdr
socket API and does not change focus. A startup hook starts it after the Herdr
API socket is ready. A supervisor listens for Codex pane lifecycle events,
reconciles against the current agent list after each subscription, and
reconnects with backoff if the event stream closes. Each Codex pane has its own
output watcher, which also reconnects independently. The `start`, `stop`, and
`status` actions manage the background watcher.

## Local development

```bash
herdr plugin link /absolute/path/to/herdr-capacity-guard --enabled
herdr plugin action invoke herdr.codex-capacity-guard.status
```

After linking into an already running server, start the watcher once with the
`herdr.codex-capacity-guard.start` action. On later server startups, the startup
hook starts it automatically.

Stop it with the `herdr.codex-capacity-guard.stop` action before disabling or
unlinking the plugin. Per-pane wait states are stored in the plugin state
directory so a restart resumes a pending ten-minute timer.

## Safety limits

- Only the exact capacity message triggers a retry.
- Only panes whose recognized agent is Codex are watched.
- The first `Продолжить` is immediate; ten minutes starts only after Codex
  returns the capacity error after that prompt.
- Repeated capacity matches during an active wait do not reset its deadline or
  send extra prompts. They keep the pane red until the scheduled retry.
- A fresh error must appear after the latest `Продолжить`; stale text above it
  is ignored when a pane watcher reconnects.
