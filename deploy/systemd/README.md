# systemd service templates (Debian/Ubuntu)

Hand-editable `.service` unit files and matching `EnvironmentFile` examples
for a bare-metal Debian 13 / Ubuntu install — the systemd equivalent of
[`deploy/openrc/`](../openrc/README.md) (Alpine has no systemd). Same 5
services, same CLI subcommands/modes, same env-silo lesson — different init
system and a different `EnvironmentFile` gotcha (no shell — see below).

## Services shipped here

| Service               | `ExecStart` subcommand             | Transport                                | Needs a public HTTPS endpoint? |
| ---------------------- | ---------------------------------- | ----------------------------------------- | ------------------------------- |
| `hivepilot-api`        | `api serve --host ... --port ...`  | HTTP                                      | Only if exposed beyond loopback |
| `hivepilot-scheduler`  | `schedule daemon --interval 30`    | —                                          | No                               |
| `hivepilot-telegram`   | `telegram start --mode polling`    | Long-poll (outbound)                      | No                               |
| `hivepilot-slack`      | `slack start --mode socket`        | Slack **Socket Mode** (outbound)          | **No**                           |
| `hivepilot-discord`    | `discord start --mode gateway`     | Discord **gateway** WebSocket (outbound)  | **No**                           |
| `hivepilot-headroom-proxy` | `headroom proxy --stateless --offline --no-cache` | HTTP on loopback | **No** — and must stay loopback |

### `hivepilot-headroom-proxy` is opt-in and ships disabled

Shipping the unit does not turn it on. It compresses tool outputs, logs and
RAG chunks in flight between the agent CLIs and the provider, which is where
coding agents actually burn tokens — a single review round was measured
reading 350k–517k cache tokens to review a 3.8 KB diff.

**Read this before enabling it.** The proxy sees every prompt, every tool
output, and the credential the client sends. Under a Claude Code
*subscription*, that credential is the **account OAuth session**, not a scoped
API key. The safety argument for running it is narrow and worth stating
plainly: the `claude` process already sees the same data, under the same user,
on the same host — so a loopback-only, write-nothing proxy adds a local
process, **not a new network boundary**. Every flag in the unit exists to keep
that true. `--log-messages` in particular would persist prompt content *and*
serve it on a live HTTP feed; on our prompts that means resolved
`${secret:NAME}` values.

Agents are pointed at it with `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`
(verified to be honoured even under OAuth). Removing that variable is the
one-line rollback.

Slack/Discord token requirements and the outbound-only rationale are
identical to the OpenRC templates — see
[`deploy/openrc/README.md`](../openrc/README.md#why-socket-mode--gateway-mode-outbound-only)
if you need the full explanation; it isn't repeated here.

## Install

```bash
# Pick the services you need — hivepilot-api and hivepilot-scheduler are
# mandatory; hivepilot-telegram/hivepilot-slack/hivepilot-discord are each
# optional and independent of one another.
sudo cp deploy/systemd/hivepilot-api.service       /etc/systemd/system/
sudo cp deploy/systemd/hivepilot-scheduler.service /etc/systemd/system/
sudo cp deploy/systemd/hivepilot-telegram.service  /etc/systemd/system/
sudo cp deploy/systemd/hivepilot-slack.service     /etc/systemd/system/
sudo cp deploy/systemd/hivepilot-discord.service   /etc/systemd/system/

sudo mkdir -p /etc/hivepilot
sudo cp deploy/systemd/env/shared.env.example              /etc/hivepilot/shared.env
sudo cp deploy/systemd/env/hivepilot-api.env.example        /etc/hivepilot/hivepilot-api.env
sudo cp deploy/systemd/env/hivepilot-scheduler.env.example  /etc/hivepilot/hivepilot-scheduler.env
sudo cp deploy/systemd/env/hivepilot-slack.env.example      /etc/hivepilot/hivepilot-slack.env
sudo cp deploy/systemd/env/hivepilot-discord.env.example    /etc/hivepilot/hivepilot-discord.env
# Edit each /etc/hivepilot/*.env file: fill in real tokens/URLs.
sudo chmod 600 /etc/hivepilot/*.env   # these hold secrets

sudo systemctl daemon-reload
sudo systemctl enable --now hivepilot-api hivepilot-scheduler hivepilot-slack hivepilot-discord

# Check status / logs
systemctl status hivepilot-api
journalctl -u hivepilot-api -f
```

## EnvironmentFile format — NOT a shell (read this before copying from OpenRC)

**`EnvironmentFile=` is parsed by systemd itself — it does not run a shell, does NOT invoke `/bin/sh`.**
This is the single biggest trap when migrating values from the OpenRC
`conf.d/*.example` files, which ARE sourced by a shell (`. /etc/conf.d/...`)
and therefore use `export KEY=value` freely.

- **`export ` does nothing useful here.** A line like `export FOO=bar` in a
  systemd `EnvironmentFile` is parsed as a literal variable named
  `export FOO` with value `bar` — not the `FOO=bar` you intended. Every
  example file in [`deploy/systemd/env/`](env/) uses plain `KEY=value`, with
  no `export` keyword.
- **`$VAR`/`${VAR}` expansion does NOT happen.** Each line is a literal
  key/value pair; there is no variable substitution, no command
  substitution, no quoting rules beyond systemd's own (see
  `systemd.exec(5)`, "EnvironmentFile=").
- **Required format:** one `KEY=value` per line. Comments start with `#`.
  Blank lines are ignored. No trailing shell syntax of any kind.

## `EnvironmentFile=-` (the leading dash)

Every unit in this directory loads two files, in order:

```
EnvironmentFile=-/etc/hivepilot/shared.env
EnvironmentFile=-/etc/hivepilot/hivepilot-<svc>.env
```

The leading `-` means "optional" — systemd will NOT fail the unit if the
file doesn't exist yet (handy while bootstrapping). `shared.env` is loaded
first so the per-service file can override it if truly needed; in practice
`shared.env` should only ever hold values that must be IDENTICAL across
every `hivepilot-*` service (see "Env silos" below).

## `WorkingDirectory=/` — the cwd trap, again

Every unit sets `WorkingDirectory=/`, deliberately mirroring OpenRC's
`cwd=/` behavior (OpenRC services also run with no inherited working
directory). A relative path in ANY config value — a config-repo clone path,
a plugin directory, `obsidian_vault`, a state-db path — resolves against
`/`, not against this repo's checkout or the unit file's own location. Every
path-shaped value in `deploy/systemd/env/*.example` is already absolute
(`/data`, `/opt/hivepilot/venv/...`) — keep it that way when you add your
own. See [`deploy/openrc/README.md`, "Env-silo warning (cwd=/)"](../openrc/README.md#env-silo-warning-cwd)
for the original production incident this guards against.

## Env silos — read this (same lesson as OpenRC, different files)

Each service above is a **separate systemd process with its own
`/etc/hivepilot/hivepilot-<svc>.env` file** — there is no environment shared
across `hivepilot-api`, `hivepilot-scheduler`, `hivepilot-telegram`,
`hivepilot-slack`, and `hivepilot-discord` beyond what you explicitly put in
`shared.env`. Setting a variable in only one service's own `.env` file has
**zero effect** on any other service.

This matters more than it looks like, because **an approval callback
(Telegram/Slack/Discord) resumes the pipeline INSIDE that bot's own
process** — not inside `hivepilot-scheduler`, which only kicked the run off.
A concrete incident on the OpenRC deployment: `IS_SANDBOX=1` was set in
`hivepilot-api`'s and `hivepilot-scheduler`'s env files (2 of 3 relevant
services) but not `hivepilot-telegram`'s. Runs that reached a checkpoint and
got resumed via a Telegram approval executed the developer stage **without**
`IS_SANDBOX`, and it failed.

**Remedy:** put every var that must apply to *every* service — anything a
resumed pipeline might need, plus any other cross-cutting setting — in
`/etc/hivepilot/shared.env` (see
[`env/shared.env.example`](env/shared.env.example)), which every unit here
already loads first via `EnvironmentFile=-/etc/hivepilot/shared.env`.

## Hardening — what's on, what's off, and why

Every unit sets `NoNewPrivileges=yes` (always safe — nothing here needs
setuid/setgid escalation). Stricter sandboxing options
(`ProtectHome=`, `ReadOnlyPaths=`, `PrivateTmp=`) are **deliberately NOT
enabled** in the shipped templates: the agents these services run write into
arbitrary project repo checkouts (often under a user's home directory), the
Obsidian vault, and `/tmp` (scratch files, git worktrees, build caches).
Turning any of those on would silently break agent runs. If you confine
`HIVEPILOT_BASE_DIR` and every project/vault path to one dedicated, non-home
directory, you can safely layer these in yourself — see `systemd.exec(5)`.

## root vs non-root, and `IS_SANDBOX`

The current Alpine box runs these services as **root**. On systemd this is
avoidable and preferable: set `User=`/`Group=` in each unit to a dedicated
non-root account (e.g. `hivepilot`), owning `/data` and the checkouts it
needs. If you keep root anyway, note that **`claude` refuses
`--dangerously-skip-permissions` under root/sudo unless `IS_SANDBOX=1` is
set** in the process environment — put `IS_SANDBOX=1` in
`/etc/hivepilot/shared.env` (never in a single service's own `.env` file,
per "Env silos" above) if you keep root.

## Restart requirements

Editing a `/etc/hivepilot/*.env` file only takes effect on the next
`systemctl restart <svc>` (systemd does not hot-reload `EnvironmentFile=` —
run `systemctl daemon-reload` too if you edited the `.service` unit itself,
then restart). See
[DEPLOY-PRODUCTION.md's "Config hot-reload" section](../../docs/DEPLOY-PRODUCTION.md#9-operations)
for which *application-level* config changes (`roles.yaml`/`projects.yaml`/
etc., synced via `hivepilot config sync`) do NOT need a daemon restart —
those are reloaded live via `hivepilot reload` / `SIGHUP` instead. A change
to any environment variable in these `.env` files (a new token, a different
allow-list) always needs a service restart, since environment variables are
only read once, at process start.

## Troubleshooting

- **Service fails to start / crash-loops:** read the actual failure first —
  `journalctl -u <svc> -n 50` (e.g. `journalctl -u hivepilot-slack -n 50`).
  Logs go straight to journald; there is no `output_log`/`error_log` file to
  find (unlike OpenRC).
- **`ModuleNotFoundError` for `slack_bolt` / `discord`:** the Slack and
  Discord bots need their optional extras installed in the venv —
  `slack-bolt` for `hivepilot-slack`, `discord.py` for `hivepilot-discord`.
  Install them in `/opt/hivepilot/venv` before enabling those units.
- **`ExecStart` binary not found / PATH-dependent tool missing (git, node,
  etc.):** systemd units get a **minimal PATH**, unlike an OpenRC `conf.d`
  file or your interactive shell — do not assume your login shell's `PATH`
  is inherited. Prefer absolute paths everywhere (the templates already use
  `/opt/hivepilot/venv/bin/hivepilot`); if an agent shells out to a tool
  only found via `PATH`, add an explicit
  `Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
  line to the unit (include `/usr/sbin:/sbin` — some distro tooling agents
  invoke lives there, and it is NOT in systemd's default minimal PATH).
- **Two Telegram pollers fighting (`Conflict: terminated by other getUpdates
  request`):** you have both the Alpine OpenRC service and this systemd
  service running against the SAME bot token. Stop one before starting the
  other — see the cutover order in the migration runbook.
