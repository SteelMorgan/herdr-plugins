# Herdr Quota

Live Herdr pane for subscription limits from Codex, Claude, Kimi and Qwen,
plus the DeepSeek API balance. The dashboard uses only the Python standard
library.

For Kimi, the dashboard reads the shared membership balance from the web
`MembershipService/GetSubscriptionStats` endpoint and renders it as `monthly`,
including the reset countdown. The browser-derived web credential is kept
in `plugins/herdr-quota/.secrets/kimi-web-token.json`; that directory is
ignored by Git, the file is mode `0600`, and an expired access token is rotated
through Kimi's refresh endpoint without being logged. If the web credential is
not available, the dashboard falls back to the optional `totalQuota` value
from Kimi Code `/usages`.

For Qwen, the dashboard reads the team Token Plan through the qwencloud.com
console gateway and renders the monthly credit quota, the reset countdown, the
per-seat credit breakdown, and a burn-rate pace. Qwen exposes no quota API, so
this uses your browser session. The cookie is kept in
`plugins/herdr-quota/.secrets/qwen-web-session.json`; that directory is
ignored by Git, the file is mode `0600`, and it is never rendered or logged.
Refresh it with `herdr plugin action invoke herdr.quota.refresh-qwen`, which
opens a dedicated Brave window (own profile, remote-debugging port) pointed at
the team analytics page. Log in there if prompted, then re-run the action: it
extracts the cookies through the browser itself — bypassing Chromium's
app-bound cookie encryption — and verifies the session before saving.

## Configure DeepSeek

Recommended: open the masked setup popup. The key is validated before it is
written and never appears in shell history or plugin logs.

```sh
herdr plugin action invoke herdr.quota.configure-deepseek
```

Manual alternative:

After linking the plugin, find its config directory with:

```sh
herdr plugin config-dir herdr.quota
```

Create `.env` there with mode `0600`:

```text
DEEPSEEK_API_KEY=your-key
```

The key is read locally and is never rendered or written to logs.

## Open

```sh
herdr plugin action invoke herdr.quota.open
```

Press `r` to refresh and `q` to close the dashboard process.
