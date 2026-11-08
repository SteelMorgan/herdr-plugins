# Herdr Agent Guard

This local Herdr plugin (id `herdr.codex-capacity-guard`) watches recognized
Codex and DSH TUI panes for retryable turn failures. On the first match it
sends `Продолжить`; while the same unresolved failure remains it retries on a
ten-minute cycle, and it clears the pane state as soon as the failure is gone.

Codex matches the exact messages
`Selected model is at capacity. Please try a different model.` and
`Error running remote compact task: stream disconnected before completion:`.
DSH TUI matches the current divider
`turn error · The engine is currently overloaded, please try again later`.
Other turn errors (authentication, invalid request, cancelled turns) are not
retried automatically.

Every five seconds, and on `idle`/`done` lifecycle events, the guard checks the
last eight terminal message blocks within the last 240 rows. A later subagent
completion does not hide an earlier error. Wrapped errors on narrow terminals
also match. A newer submitted user prompt supersedes an older error; the live
composer and its ghost hint are not treated as a submitted message. Recent ANSI
reads inspect the terminal tail without changing focus or driving the program's
scroll interface.

For a DSH `turn error`, the divider must still be the latest transcript
activity. Queue rows (`⏳`, `↳`, `Alt+↑`), the composer and the footer are
ignored; a later `⚓`, `•`, `●`, `■`, `✉`, `✓`, `🌓`, or submitted
`›`/`❯` row means the error
belongs to an older turn and is not retried. The guard's own submitted
`Продолжить` is not new activity: the divider stays current until the retry
produces thinking/tool/assistant output or another turn error. This prevents a
stale divider from keeping the retry loop alive after the agent has moved on.

When a Codex retry is sent, the guard stores a SHA-256 fingerprint of the latest
submitted prompt, not the prompt text. If a different user prompt is later
submitted and that new turn ends in a capacity error, its first retry is sent
immediately even if an older retry was still recorded as pending. The guard's
own `Продолжить` is not treated as a new user turn; an error after it retains
the ten-minute cooldown. If the earlier prompt is no longer visible, the guard
does not infer a new turn from missing evidence.

Continue is sent only while Herdr recognizes the target as `idle` or `done`.
The `done` status means unseen idle output, not proof that the task succeeded:
it does not cancel a retry timer by itself. A due retry waits while the agent is
working, blocked, or unknown. Periodic scans recover missed output events.

For mirrored panes, Herdr may report `agent_not_ready` because the foreground
process on the central side is the mirror client. For a Docker mirror, the
plugin reads the destination from the actual mirror process, verifies the
remote pane is the expected idle/done agent, and submits through the remote
Herdr agent API. A failed or timed-out remote request never falls back to
terminal input: its delivery may be ambiguous. Other mirror transports retain
the existing terminal fallback. A failed send marks the pane for attention and
raises a notification. The plugin stores each pane's retry phase and deadline so
a Herdr restart preserves an active wait.

The watcher targets panes Herdr identifies as `codex` or `dsh-tui`. It uses the
Herdr socket API and does not change focus. A startup hook starts it after the
Herdr API socket is ready. A supervisor listens for watched-agent lifecycle
events, reconciles against the current agent list after each subscription, and
reconnects with backoff if the event stream closes. Each watched pane has its
own output watcher, which also reconnects independently. The `start`, `stop`,
and `status` actions manage the background watcher.

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

- Only the exact capacity messages, the specific remote compact stream-error
  prefix, or the DSH overload `turn error` divider triggers a retry; unrelated
  stream, transport, authentication or validation errors do not.
- Only panes whose recognized agent is `codex` or `dsh-tui` are watched.
- The first `Продолжить` is immediate; ten minutes starts only after the error
  returns or remains unresolved after that prompt.
- Repeated matches during an active wait do not reset its deadline or send extra
  prompts. They keep the pane marked until the scheduled retry.
- A DSH error followed by new assistant/tool/user activity is stale and is not
  retried.
