# Herdr Quota

Live Herdr pane for subscription limits from Codex, Claude and Kimi, plus the
DeepSeek API balance. The dashboard uses only the Python standard library.

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
