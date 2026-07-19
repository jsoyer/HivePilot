# Integrations

HivePilot integrates with chat platforms (for remote control and notifications), issue
trackers, Obsidian, a reverse proxy, n8n, and SSH remote hosts. Most of these require an
optional extra or credentials and are off until configured.

## Telegram (remote control + notifications)

Requires `pip install "hivepilot[notifications]"` plus:

- `HIVEPILOT_TELEGRAM_BOT_TOKEN` — bot token from `@BotFather`
- `HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS` — JSON array of allowed chat IDs; empty means open

Start the bot:

```bash
hivepilot telegram start
```

This is long-running — run it under a process supervisor or systemd.

Helper commands:

```bash
hivepilot telegram chat-id        # look up a chat ID
hivepilot telegram info           # show bot/webhook info
hivepilot telegram set-webhook    # configure a webhook (mutates Telegram's config)
hivepilot telegram delete-webhook # remove the webhook (destructive)
hivepilot telegram systemd-unit   # print a systemd unit file for `telegram start`
```

Capabilities: run pipelines/tasks, inspect steps and interactions, approve gated actions,
and live-stream each agent turn back to the chat. Live streaming is controlled by
`HIVEPILOT_TELEGRAM_STREAM_LIVE`, optionally routed to a dedicated
`HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID`. Free-text `@mentions` (per-agent aliases) route a
message directly to a specific agent. Exact command names are chat commands the bot exposes
for running a pipeline, listing steps/interactions, and approving gated actions — check
`telegram info` or the bot's own help output for the current set.

## Slack

```bash
hivepilot slack start    # ChatOps bot (long-running)
hivepilot slack notify   # send a one-off message
```

`slack start` uses Slack app/bot credentials (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`SLACK_SIGNING_SECRET`). Notifications can also go through a webhook (`SLACK_WEBHOOK_URL`).

## Discord

```bash
hivepilot discord start    # bot (long-running)
hivepilot discord notify   # send a one-off message
```

Webhook-based notifications use `DISCORD_WEBHOOK_URL`.

## Notifiers (webhooks)

Notifications are a plugin contribution type. Webhook URLs (`SLACK_WEBHOOK_URL`,
`DISCORD_WEBHOOK_URL`) drive run notifications independent of the chat bots above. All
notification content is secrets-masked before it is sent. See
[PLUGINS.md](PLUGINS.md) for the plugin contribution model and
[SECURITY.md](SECURITY.md) for the masking guarantees.

## Notion

```bash
hivepilot notion status   # read-only status check
hivepilot notion setup    # writes Notion config
hivepilot notion sync     # mutates the connected Notion workspace
```

Requires Notion credentials (integration token and target database/page IDs).

## Linear

```bash
hivepilot linear teams    # read-only: list teams
hivepilot linear states   # read-only: list workflow states
hivepilot linear issue    # create/update an issue (mutates)
hivepilot linear sync     # sync run state to Linear (mutates)
```

Requires Linear credentials (API key).

## Obsidian

Ships as a plugin (notifier + lifecycle hooks). It writes run notes and changelogs to an
Obsidian vault; a pipeline stage with `commits_vault: true` triggers a vault changelog commit
after that stage runs.

```bash
hivepilot obsidian audit   # audit the vault
```

Configure the vault path via `HIVEPILOT_OBSIDIAN_VAULT`. Vault content written by HivePilot
is secrets-masked. See [PLUGINS.md](PLUGINS.md) for how the Obsidian plugin registers its
notifier and hooks.

## Caddy (reverse proxy for the API)

Manage a Caddy reverse proxy in front of the HTTP API:

```bash
hivepilot caddy generate   # write a Caddyfile
hivepilot caddy setup      # install/configure Caddy
hivepilot caddy reload     # reload the running Caddy config
hivepilot caddy status     # show proxy status
hivepilot caddy logs       # tail proxy logs
hivepilot caddy show       # print the current Caddyfile
hivepilot caddy teardown   # remove the proxy config (destructive)
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for where Caddy fits in a deployed setup.

## n8n

The repo ships example n8n workflows under `examples/n8n/` (approve and events exports)
that call HivePilot's HTTP API for approvals and event delivery. Import those workflows into
n8n and point them at your HivePilot API instance — see [DEPLOYMENT.md](DEPLOYMENT.md) for
API setup.

## SSH remote agents

A role — or a policy `role_overrides` entry — can set a `host`, which runs that role's agent
on a remote host over SSH instead of locally. This lets different pipeline stages execute on
different machines. Configure the target host via `~/.ssh/config` and tune the SSH invocation
with `HIVEPILOT_SSH_OPTIONS`. See [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md) for role
configuration and [CONFIGURATION.md](CONFIGURATION.md) for environment variables.

## See also

- [CLI-REFERENCE.md](CLI-REFERENCE.md)
- [DASHBOARD.md](DASHBOARD.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [SECURITY.md](SECURITY.md)
