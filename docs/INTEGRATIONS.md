# Integrations

HivePilot integrates with chat platforms (for remote control and notifications), issue
trackers, Obsidian, a reverse proxy, n8n, and SSH remote hosts. Most of these require an
optional extra or credentials and are off until configured.

## Telegram (remote control + notifications)

Requires `pip install "hivepilot[notifications]"` plus:

- `HIVEPILOT_TELEGRAM_BOT_TOKEN` — bot token from `@BotFather`
- `HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated or JSON array of allowed chat IDs, e.g. `123456,789012` or `[123456,789012]`; empty means open

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

## Signal

Signal has **no cloud bot API** and, being end-to-end encrypted peer-to-peer messaging,
**no inbound webhook mode** — unlike Telegram/Slack/Discord above, there is nothing for
Signal to push updates to. A Signal bot is a dedicated phone number driven by either the
[`signal-cli`](https://github.com/AsamK/signal-cli) binary or its HTTP wrapper,
[`signal-cli-rest-api`](https://github.com/bbernhard/signal-cli-rest-api); messages are
received by **polling** (`signal-cli --output=json receive`, or `GET /v1/receive/{number}`
against the REST wrapper) and sent the same way each mode's send call.

```bash
hivepilot signal start --mode cli    # or --mode rest — blocking poll loop
hivepilot signal notify "message"    # one-off notification
hivepilot signal register +15551234567 [--voice] [--captcha <token>]
hivepilot signal link                # link this machine as a secondary device
hivepilot signal info                # show config + signal-cli PATH availability
```

One-time setup (either path — register a *new* number, or link as a secondary device off
an existing Signal account):

- **Register**: `hivepilot signal register <number>` triggers `signal-cli register`
  (SMS or `--voice` call); once you receive the code, finish with
  `signal-cli -a <number> verify <code>` directly (not wrapped by HivePilot, since it
  needs the human-received code as input).
- **Link**: `hivepilot signal link` prints a `sgnl://linkdevice?...` URI for your primary
  phone to scan (Signal app → Linked Devices → Link New Device).

`signal-cli` persists its registration state (identity keys, session data) in its own
local data directory — set up once via register/link, never passed as a CLI secret
afterward. On Alpine: `apk add openjdk17-jre` plus the `signal-cli` release jar/tarball
on `PATH`; alternatively, run the `signal-cli-rest-api` container and point
`HIVEPILOT_SIGNAL_REST_URL` at it (`--mode rest`), which needs no JVM on the HivePilot
host itself.

Configuration: `HIVEPILOT_SIGNAL_NUMBER` (the bot's own E.164 number),
`HIVEPILOT_SIGNAL_ALLOWED_NUMBERS` (comma-separated or JSON array E.164 whitelist, e.g.
`+15551234567,+15557654321`; empty means open),
`HIVEPILOT_SIGNAL_CLI_PATH` (defaults to `signal-cli` on PATH),
`HIVEPILOT_SIGNAL_REST_URL` (base URL of a running `signal-cli-rest-api`, rest mode only),
`HIVEPILOT_SIGNAL_NOTIFICATION_NUMBER` (proactive notifications),
`HIVEPILOT_SIGNAL_RECEIVE_MODE` (`cli` or `rest`, default `cli`).

Commands mirror the other bots (`/run`, `/approvals`, `/approve`, `/deny`, `/status`,
`/help`) via the same shared `chatops_service` dispatch layer. Signal has no inline
buttons, so approvals are reply-driven: `approve <run_id>` / `deny <run_id> [reason]`
works with or without a leading `/`. A missing `signal-cli` binary (or an unreachable
REST endpoint) degrades gracefully — a clear error on `signal start`/`signal notify`, and
a logged skip-and-continue on each poll tick — it never crashes a HivePilot run.

## Natural-language concierge (opt-in)

By default, chatting with HivePilot means memorizing commands (`/run`, `/ask`, `@mention`).
The concierge lets you talk to it like a colleague instead — a plain-text message is
classified into **ANSWER** (a direct reply, e.g. "what's running?"), **ROUTE** (address a
specific agent, e.g. "ask Gustave to fix the auth bug"), or **ACTION** (an orchestration
primitive: run a task/pipeline, approve/deny a run). It is **off by default** and
**fail-closed**: any classifier error, timeout, or malformed response degrades to a
friendly "I didn't quite get that" answer — it never fabricates or guesses an action it
can't validate.

Enable it:

```bash
export HIVEPILOT_CHATOPS_CONCIERGE_ENABLED=true   # opt-in, default OFF
export HIVEPILOT_CHATOPS_DEFAULT_ROLE=ceo          # role addressed when the user doesn't name one (default: ceo)
export HIVEPILOT_CHATOPS_CONCIERGE_MODEL=haiku     # cheap/fast classifier model (default: a built-in cheap model)
```

How it behaves once enabled:

- **Telegram**: a plain (non-`@`, non-`/`) message that used to be silently ignored is now
  classified. An **answer** replies directly. A **route**/**action** (routing to an agent,
  running a task/pipeline, or approving/denying a run) is always treated as destructive and
  shows a ✅ Yes / ❌ No inline keyboard before anything runs.
- **Signal** (and the generic `/chatops/*` webhook path, which shares the same dispatch
  layer): free text that doesn't match a known command is classified the same way. An
  **answer** is returned directly. A destructive route/action returns a text confirmation
  — `⚠️ This will <summary>. Reply 'yes <token>' to confirm or 'no' to cancel.` — since
  Signal has no inline buttons.
- **Every route/action is destructive by design** — there is no unconfirmed path from
  free text to an actual run, pipeline trigger, or approval/denial. A read-only request
  ("what's pending?", "who is the CTO?") is always answered directly, never turned into a
  fake "read action".
- Confirming re-checks the same ChatOps-token permission level the equivalent explicit
  command would require (`run` for routing/running, `approve` for approving/denying) — the
  confirmation step never bypasses existing authorization.

The concierge runs one LLM classification call per free-text message when enabled — be
mindful of cost/latency on high-traffic chats; the default model is intentionally a cheap
one for this reason.

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
