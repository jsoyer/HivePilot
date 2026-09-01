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
Approval messages carry Approve / Deny / **Challenge / Ask** buttons (Block Kit) — pressing
Challenge / Ask prompts for a plain-text follow-up message, which is routed to the Chief of
Staff (the run stays paused) via the same shared entrypoint Telegram uses. The follow-up
reply is bound to the user who pressed the button and expires after ~15 minutes if never
answered. Challenge/Ask's plain-text follow-up requires subscribing the app to the
`message.channels` (and/or `message.im`/`message.groups`, depending on where the bot is
invited) event in the Slack app configuration — the SAME event subscription the
natural-language concierge needs (see below) — without it, the button opens fine but the
follow-up reply is never delivered. Restrict to specific channels with
`HIVEPILOT_SLACK_ALLOWED_CHANNEL_IDS` — **if left unset, every channel the bot is a member
of can approve, deny, and challenge runs** (empty allow-list means open, not closed).

## Discord

```bash
hivepilot discord start    # bot (long-running)
hivepilot discord notify   # send a one-off message
```

Webhook-based notifications use `DISCORD_WEBHOOK_URL`. Approval messages carry Approve /
Deny / **Challenge / Ask** buttons — pressing Challenge / Ask opens a modal (no privileged
Message Content intent required); the submitted text is routed to the Chief of Staff (the
run stays paused) via the same shared entrypoint Telegram/Slack use. **These buttons work
identically in both `discord start --mode webhook` (HTTP interactions) and `--mode gateway`
(WebSocket, the default)** — gateway mode routes MESSAGE_COMPONENT/MODAL_SUBMIT interactions
through the SAME dispatcher the webhook path uses, delivering the response via Discord's
interaction-callback REST endpoint rather than an HTTP response body. Restrict to specific
guilds/channels with `HIVEPILOT_DISCORD_ALLOWED_GUILD_IDS` / `HIVEPILOT_DISCORD_ALLOWED_CHANNEL_IDS`
— **if both are left unset, every guild/channel the bot can see can approve, deny, and
challenge runs** (empty allow-list means open, not closed).

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
export HIVEPILOT_CHATOPS_CONCIERGE_MODE=api        # "api" (default) or "cli" — see below
```

**No `ANTHROPIC_API_KEY`? The concierge still works.** `HIVEPILOT_CHATOPS_CONCIERGE_MODE`
controls how the classifier's `claude` call is dispatched:

- `api` (default) — the Anthropic Messages API, using the cheap/fast model above. Cheapest
  and fastest option **when an API key is available**.
- `cli` — the operator's local `claude` CLI (subscription/OAuth-authenticated), so **no
  `ANTHROPIC_API_KEY` is required at all**. This is the mode a subscription-only deployment
  (e.g. the bare-metal/Alpine install running `claude` interactively via a Claude
  subscription, no API key configured) needs.
- Left at the default `api` with no `ANTHROPIC_API_KEY` present in the environment, the
  concierge **automatically falls back to `cli`** the first time it classifies a message —
  logging it once — so a turn-key subscription-only box works with zero extra
  configuration. Set `HIVEPILOT_CHATOPS_CONCIERGE_MODE=cli` explicitly to always use the CLI
  path regardless of whether a key is present.
- **`cli` mode runs the classifier with NO tools available at all** (`claude --tools ""`) and
  never with an elevated permission mode (no `bypassPermissions`). The classifier's prompt
  embeds the free-text chat message being classified — untrusted, potentially
  attacker-controlled input — so the session it runs in must be structurally incapable of
  invoking Bash/Edit/WebFetch/etc, not merely permission-gated; a prompt-injected instruction
  has nothing to invoke. This is a hard invariant enforced in code: if the no-tools
  restriction can't be attached to a `cli`-mode request for any reason, the concierge refuses
  to run it and returns the fail-closed answer instead of ever spawning a tool-capable session.

How it behaves once enabled:

- **Telegram**: a plain (non-`@`, non-`/`) message that used to be silently ignored is now
  classified. An **answer** replies directly. A **route**/**action** (routing to an agent,
  running a task/pipeline, or approving/denying a run) is always treated as destructive and
  shows a ✅ Yes / ❌ No inline keyboard before anything runs.
- **Slack**: any plain channel message (not a slash command) is classified the same way. An
  **answer** replies directly in-channel. A destructive route/action posts a Block Kit
  message with Yes / No buttons before anything runs. Requires subscribing the app to the
  `message.channels` (and/or `message.im`/`message.groups`, depending on where the bot is
  invited) event in the Slack app configuration so `message` events are delivered.
- **Discord**: **gateway mode only** — the HTTP-interactions mode can't receive plain
  messages, so this doesn't apply to a bot run via `discord` HTTP webhooks. A plain channel
  message is classified the same way. An **answer** replies directly. A destructive
  route/action replies with a text confirmation — `⚠️ This will <summary>. Reply
  'yes <token>' to confirm or 'no' to cancel.` — mirroring Signal's text-only confirmation
  flow. Reading plain message content requires the privileged **Message Content Intent**:
  enable it for the bot in the [Discord developer portal](https://discord.com/developers/applications)
  (Bot → Privileged Gateway Intents → Message Content Intent) — without it, Discord silently
  withholds message content even though the gateway connection succeeds. This intent is only
  requested by `discord gateway` when `HIVEPILOT_CHATOPS_CONCIERGE_ENABLED=true`.
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
  confirmation step never bypasses existing authorization. Every platform whitelists
  (allowed channel/chat/guild) before the concierge is ever reached, and ignores the bot's
  own messages / non-plain message subtypes to avoid loops.

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

### Which vault? (per-project destination)

`HIVEPILOT_OBSIDIAN_VAULT` is the machine-wide **default**. A project can route its own
artifacts elsewhere with `obsidian_vault:` in projects.yaml — HivePilot's own work to a
personal vault, a product pipeline's work to the project vault:

```yaml
projects:
  hivepilot:
    path: /home/jerome/code/hivepilot
    obsidian_vault: /home/jerome/vaults/personal
  noxys:
    path: /home/jerome/code/noxys
    obsidian_vault: /home/jerome/vaults/noxys
```

Every writer in the table below, plus the plugin's `recall` read path and the
`{OBSIDIAN_VAULT}` prompt variable, resolves through the same function
(`hivepilot/services/obsidian_vault_resolver.py`), so reads and writes can never target
different vaults. A project with no `obsidian_vault:` key keeps using the global setting
unchanged. The override must be absolute and must already exist — HivePilot never creates
a vault directory. Full rules (including why an empty or relative value refuses to load):
[CONFIGURATION.md → Per-project Obsidian vault](CONFIGURATION.md#per-project-obsidian-vault--obsidian_vault).

### Vault layout (where HivePilot writes)

HivePilot writes into a small, fixed set of folders below the vault root. Every write goes
through `ObsidianService`, which confines each writer to one folder and rejects any path that
would escape it. Both the vault *root* **and the folder names** are configurable: the names
come from the `folders:` key of `vault.yaml` (see
[CONFIGURATION.md → `vault.yaml`](CONFIGURATION.md#vaultyaml--your-vaults-folder-taxonomy)),
because a vault's filing convention belongs to the organisation that owns the vault. Below,
`<slot>` denotes the folder name you declared for that slot.

| Vault path | Written by | Folder slot / builder |
| --- | --- | --- |
| `<artifacts>/<role>/<date>-<slug>.md` | `ObsidianService.write_artifact()` — the canonical, human-facing copy of a stage deliverable, filed by the producing role | `artifacts` slot |
| `<decisions>/<date>-<slug>.md` | `ObsidianService.write_adr()` — architecture decision records | `decisions` slot |
| `<hivepilot>/Runs/<date>-run<id>-<stage>.md` | `pipelines.write_stage_artifact()` — the internal per-stage run log | `hivepilot` slot + `_RUNS_SUBFOLDER` (`hivepilot/pipelines.py`) |
| `<hivepilot>/Runs/<date>.md` | Obsidian plugin daily journal (`append_daily`) | `hivepilot` slot |
| `<hivepilot>/Interactions/` | Per-stage interaction log | `hivepilot/services/interaction_service.py` |
| `<hivepilot>/Audit/` | Auditor observations and proposals | `hivepilot/services/auditor_service.py` |
| `<hivepilot>/Docs/changelog-run-<id>.md` | Stages with `commits_vault: true` | `hivepilot/orchestrator.py` |

The subfolders *inside* the `hivepilot` subtree (`Runs`, `Interactions`, `Agents`, `Tasks`,
`Reports`, `Audit`, `Docs`) are engine-owned and not configurable — they are HivePilot's own
workspace structure, not part of any organisation's taxonomy.

**Unconfigured slots refuse.** An undeclared `artifacts` or `decisions` slot makes the
corresponding write raise `ObsidianWriteError`. It is never redirected to the vault root and no
folder name is invented — `_emit` calls `mkdir(parents=True)`, so a guessed name would not be a
harmless miss but a brand-new top-level folder in what is typically a synced git repo. The
`hivepilot` slot is the one with an engine default (`HivePilot`), so run logs keep working in a
deployment that configures nothing else.

**Stage deliverables (`<artifacts>/`).** For each pipeline stage, HivePilot writes *two*
copies: the internal run log under `<hivepilot>/Runs/`, and — when the stage's task
declares a `role` — a canonical deliverable under `<artifacts>/<role>/`. The chain is
`Orchestrator.run_pipeline` → `pipelines.write_stage_artifact(..., role=...)` →
`ObsidianService.write_artifact()`. The `role` subfolder is the task's role string, slugified
so an unexpected value cannot introduce path separators.

HivePilot writes this copy itself rather than asking the agent to write it, because agents run
headless (`--print`), where the Write tool is unavailable and the vault is outside the agent's
accessible directories. Prompts should therefore *describe* the artifact, not try to save it.

Two cases are skipped deliberately: the `developer` role (its deliverable is a code diff that
belongs in the project's git repo, not the vault) and blank stage output (no empty artifact
files). A stage whose task declares no role gets the run-log copy only. The artifact copy is
best-effort — a vault write failure is logged (`obsidian.artifact_write_failed`) and never
fails or pauses the run. An undeclared `artifacts` slot is one such failure: the run continues
and the miss is logged.

**Audit-only folders.** `hivepilot obsidian audit` also reports on folders HivePilot never
writes to — whatever you list under `expected_folders:` in `vault.yaml`. Their presence in that
part of the audit report means "expected in this vault layout", **not** "HivePilot writes here".
Only the folders in the table above receive engine writes.

**The two lists are independent, on purpose.** `expected_folders:` (your declared layout) is not
derived from `folders:` (where the engine writes), and vice versa. They answer different
questions, and most `expected_folders:` entries have no writer at all. The audit reports the
engine's own folders in a *separate* `engine_folders` section, derived from the slot vocabulary
itself, so it is structurally incapable of omitting a write target no matter what you declared.
That matters because of a real past bug: the artifacts folder — the one folder the engine writes
deliverables into — was absent from the audit's expected list for several releases, so the audit
reported a complete-looking vault while the engine wrote somewhere the operator was never told
about. Unioning the two lists would have papered over the distinction rather than fixing it.

**An audit that checks nothing is not a pass.** `expected_folders:` has no engine default —
HivePilot has no opinion on how an organisation files its vault. A deployment that declared none
is told so explicitly (`Declared layout: NOT CHECKED …`), and `hivepilot obsidian audit --strict`
exits 1 when it examined zero folders or found an unconfigured engine slot. Same rule
`hivepilot plugins audit --strict` follows.

**Other vault layouts.** Nothing above assumes a particular naming convention. Declare your own
names in `vault.yaml` (`examples/vault.yaml` is a starting point), or leave the vault integration
disabled entirely (`HIVEPILOT_OBSIDIAN_ENABLED=false`).

## herdr (live role board)

Watch roles work as they work, in a terminal multiplexer, instead of reading
about it afterwards in Telegram or Slack.

```bash
hivepilot herdr board --roles reviewer,ciso,qa | herdr layout apply
```

One pane per role, each running `hivepilot watch --role <name>`.

### Why the pane runs a follower and not the agent

herdr has no PTY-less pane. A `pane` node without `command` starts a shell
rather than becoming a passive surface, and popups carry no `pane_id` so
they cannot be addressed. A pane must run *something* — so the board gives
it the cheapest stable thing there is.

That constraint produces the better design rather than a compromise:

- **The board stands between runs.** A follower outlives any pipeline;
  panes running agents would blink in and out.
- **Restoring a layout is safe.** herdr recreates terminals on
  `layout.apply` and the followers simply re-attach. Had the panes run the
  agents, restoring a saved layout would **re-execute them** — a saved
  layout would become a re-run trigger.
- **Nothing about execution changes.** Cost capture, verdict parsing and
  runner resolution stay exactly where they are.

### Remote deployments

The board runs where you are; the services run on the box. `--stdin` bridges
that with no new network surface — nothing new listens anywhere:

```bash
hivepilot herdr board --roles ciso \
  --remote noxysdevbot --log-path /runs/logs/hivepilot.log
```

### What is safe to display

The stream is untrusted text bound for a terminal: agent output is quoted
verbatim into `detail`. Every rendered field has its control characters
stripped, including under `--json` — piping does not make bytes safe, the
other end is usually another terminal.

That strip also prevents line forgery. The board reads one record per line,
so an embedded newline in agent output could otherwise fabricate a line that
looks like an event the system emitted.

Secrets are handled upstream: every field of every event passes through the
redaction processor before it is written.

See [`watch` and `herdr board`](./CLI-REFERENCE.md#watch) for flags, and the
banner that tells you which log file you are actually following.

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

## MCP command center (HP-76)

Pollen's **MCP** tab (System group) is a unified servers + catalog page:

- **Paste-anything import** — Claude/Cursor `{"mcpServers": …}` JSON, a bare `https://…` URL, or a command (`npx -y @modelcontextprotocol/server-filesystem /tmp`). Admin-only (`POST /v1/mcp/import`).
- **Literal secrets are stripped** — only `${env:NAME}` refs are stored.
- **URLs are never fetched** on import (SSRF). A non-loopback HTTP server is recorded as `remote` and not probed; loopback GETs are allowed.
- **Health** — `shutil.which` for stdio commands; refreshed on `GET /v1/mcp/servers` (60s TTL) and via **Probe**.
- **Cost per server** is not metered yet (HP-73 tracks LLM providers, not MCP tool calls).

Catalog entries are templates (filesystem, github, fetch, memory, token-savior). Adding one writes a registry row; it does not install npm/pip.

## See also

- [CLI-REFERENCE.md](CLI-REFERENCE.md)
- [DASHBOARD.md](DASHBOARD.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [SECURITY.md](SECURITY.md)
