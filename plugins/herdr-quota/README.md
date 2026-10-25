# Herdr Quota

Live Herdr pane for Codex and Claude subscription limits plus the DeepSeek API
balance. Kimi, GLM and Qwen collectors remain in source but are disabled and
omitted from the live dashboard and quota snapshots while those subscriptions
are not monitored.

## MiMo (Token Plan)

MiMo uses a **billing-month** budget (from `tokenPlan/detail` `currentPeriodEnd`,
e.g. 5th–5th — not the calendar 1st). The dashboard shows two rows:

| Row | Budget | Meaning |
| --- | --- | --- |
| `weekly` | monthly / 4 + carry/−debt | credit/debt pool vs this week |
| `monthly` | monthly as-is | billing-month burn |

Weekly pool: unused budget rolls into next week's base; overspend becomes debt
and can show **100%+** (bar is capped, number is honest). Plan line is compact:
`1.20B/82B · week 20.50B`.

Configure in the plugin config `.env` (`herdr plugin config-dir herdr.quota`):

```text
MIMO_API_KEY=...                 # optional; else ~/.local/share/mimocode/auth.json
MIMO_MONTHLY_USED=84
MIMO_MONTHLY_LIMIT=200
```

`.env` is a **fallback snapshot**. When live cookies work the collector
auto-rewrites `MIMO_MONTHLY_USED/LIMIT` so a dead session still shows recent
numbers. Weekly reset follows the billing week when `currentPeriodEnd` is known.

### Live cookies (preferred)

Token Plan quota is read from the logged-in console:

- `GET https://platform.xiaomimimo.com/api/v1/tokenPlan/usage`
- `GET https://platform.xiaomimimo.com/api/v1/tokenPlan/detail`

`api-platform_serviceToken` lives ~6 hours. A 401 also wipes `slh`/`ph`, so the
session cannot be revived from the same cookies. Google OAuth rejects
Playwright/automation Chrome ("browser may not be secure").

**Use real Chrome/Brave + CDP** (same pattern as Qwen):

```sh
plugins/herdr-quota/bin/refresh-mimo-cookies.sh
# Chrome/Brave opens with a dedicated profile — sign in once with Google.
```

Later, `--auto` silently renews from that profile (long-lived `passToken`
mints a fresh `serviceToken`). The dashboard calls `--auto` itself when the
API returns nothing (at most every 30 min). Plugin action: **Quota: refresh
MIMO session cookies**.

Fallback if you prefer DevTools — copy Cookie from
`platform.xiaomimimo.com` → Network → `/api/v1/tokenPlan/*` → Request Headers:

```sh
plugins/herdr-quota/bin/capture-mimo-cookies.sh --stdin
# paste the cookie header, Ctrl-D
```

Secrets land in `plugins/herdr-quota/.secrets/mimo-web-session.json` (0600).
When cookies are present the collector prefers them over `.env`.

## Kimi support (disabled)

The Kimi collector and token handling remain in source, but are not run or
shown in the live dashboard or quota snapshots while this subscription is not
monitored. If re-enabled, the collector reads the shared membership balance
from the web `MembershipService/GetSubscriptionStats` endpoint and renders it
as `monthly`, including the reset countdown. The browser-derived web credential
is kept in `plugins/herdr-quota/.secrets/kimi-web-token.json`; that directory is
ignored by Git, the file is mode `0600`, and an expired access token is rotated
through Kimi's refresh endpoint without being logged. If the web credential is
not available, the collector falls back to the optional `totalQuota` value
from Kimi Code `/usages`.

## Qwen support (disabled)

The Qwen collector and cookie-refresh implementation remain in the source for
possible future use, but the dashboard does not run or display them and the
refresh action is disabled.

## GLM support (disabled)

The GLM collector remains in source but is not run or shown in the live
dashboard or quota snapshots while this subscription is not monitored. If
re-enabled, it reads the Z.ai Coding Plan quota from
`api.z.ai/api/monitor/usage/quota/limit` and renders both enforced windows —
`5 hour` and `weekly` — with reset countdowns, the plan level, and a
burn-rate pace. Z.ai returns each window as a `(unit, number)`
pair; only units confirmed against the live API are mapped, so an unrecognised
window is shown without a duration rather than with an invented pace.

## Configure GLM if re-enabled

The collector reuses the Z.ai key that DSH already holds in
`~/.dsh/.credentials.yaml` under `refs.ZAI_API_KEY`, so a DSH-connected GLM
provider needs no extra setup. To override it, or on a host without DSH, set
`ZAI_API_KEY` in the plugin `.env` next to `DEEPSEEK_API_KEY`:

```text
ZAI_API_KEY=your-key
```

`ZAI_API_KEY` in the environment wins, then the plugin `.env`, then the DSH
credential. The key is sent only to Z.ai and is never rendered or logged.

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

## Agent quota snapshots

After each refresh, the dashboard atomically writes `usage.json` in its plugin
config directory. It contains normalized spent/remaining percentages, reset
times, provider status and observation timestamps; credentials and raw collector
errors are excluded. `max_age_seconds` is 900. Consumers must assess both snapshot
age and each provider's age/status, and request fresh data after a reset passes.

Optional `export-targets.json` in the same config directory is an array of
`{"container":"dev-era-platform-dev","user":"vscode"}` objects. The dashboard
delivers the same snapshot using `docker exec` to the target user's
`~/.local/state/llm-usage/snapshot.json`. No listener or credentials are installed
in the container. Each delivery has a 10-second timeout; failures leave the last
snapshot intact and are reported in `export-status.json`. Delivery retries on
the next dashboard refresh. The dashboard must remain running for updates.

ERA provides `llm-usage --json` (or `llm-usage` for a compact summary), with the
maintained skill and reader under
`/workspaces/dev-era-platform/.framework/skills/llm-usage`. Its runtime mirror is
generated by the framework sync tool and linked into the user's Codex skills.
