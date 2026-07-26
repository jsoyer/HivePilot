# OpenRC service templates (Alpine Linux)

Hand-editable `openrc-run` service scripts and matching `conf.d` env-file
examples for a bare-metal Alpine install (`scripts/install-alpine.sh`).
Alpine has no systemd, so `hivepilot api systemd-unit` / `hivepilot schedule
systemd-unit` do not apply here — these are the reproducible equivalent.

For a scripted, idempotent alternative that generates most of these for you
(currently `hivepilot-api` + `hivepilot-scheduler` + `hivepilot-telegram`
only — Slack/Discord are not yet wired into that generator, copy the
templates below by hand for those two), see `scripts/setup-openrc.sh` and
[docs/DEPLOY-PRODUCTION.md](../../docs/DEPLOY-PRODUCTION.md).

## Services shipped here

| Service               | `command_args`                    | Transport                        | Needs a public HTTPS endpoint? |
| ---------------------- | ---------------------------------- | --------------------------------- | ------------------------------- |
| `hivepilot-api`        | `api serve --host ... --port ...`  | HTTP                              | Only if exposed beyond loopback |
| `hivepilot-scheduler`  | `schedule daemon --interval 30`    | —                                 | No                               |
| `hivepilot-telegram`   | `telegram start --mode polling`    | Long-poll (outbound)              | No                               |
| `hivepilot-slack`      | `slack start --mode socket`        | Slack **Socket Mode** (outbound)  | **No**                           |
| `hivepilot-discord`    | `discord start --mode gateway`     | Discord **gateway** WebSocket (outbound) | **No**                    |

## Why Socket Mode / gateway mode (outbound-only)

Both `hivepilot-slack` and `hivepilot-discord` default to their **outbound-only**
transport:

- **Slack Socket Mode** — the bot opens a WebSocket connection *out* to
  Slack; Slack never connects *in* to your host.
- **Discord gateway mode** — the bot opens a WebSocket connection *out* to
  Discord's gateway; same shape.

This is the right default for a box sitting behind Tailscale/a NAT/no public
IP — there is nothing to open a port for, nothing to put a reverse proxy or
TLS certificate in front of, and no `Interactions Endpoint URL` /
`Request URL` to register anywhere. The alternative `webhook` mode (both
bots support `--mode webhook` too, served off the existing `hivepilot-api`
FastAPI app) DOES require a publicly reachable HTTPS endpoint and is not
covered by the templates in this directory — use it only if you already have
a reverse proxy + TLS terminating in front of `hivepilot-api` (see
[DEPLOYMENT.md#reverse-proxy-caddy](../../docs/DEPLOYMENT.md#reverse-proxy-caddy)).

## Slack token requirements (Socket Mode)

`hivepilot slack start --mode socket` needs **all three** of the following —
the underlying `slack-bolt` `App` is constructed with the bot token and
signing secret unconditionally (both modes), plus the Socket-Mode-only
app-level token:

- `HIVEPILOT_SLACK_BOT_TOKEN` (`xoxb-...`)
- `HIVEPILOT_SLACK_SIGNING_SECRET`
- `HIVEPILOT_SLACK_APP_TOKEN` (`xapp-...`, Socket Mode only)

## Discord token requirements (gateway mode)

`hivepilot discord start --mode gateway` only needs:

- `HIVEPILOT_DISCORD_BOT_TOKEN`

`HIVEPILOT_DISCORD_PUBLIC_KEY` (the Ed25519 public key) is **only** used by
`--mode webhook`'s HTTP-interaction signature verification — leave it unset
for the default gateway mode.

## Slack / Discord require optional pip extras

Neither `slack-bolt`/`slack-sdk` nor `discord.py`/`PyNaCl` are installed by
default — install the matching extra before starting the service, or it
fails fast with a clear error:

- **Slack**: `pip install hivepilot[slack]` (pulls in `slack-bolt` +
  `slack-sdk`). Missing it fails with `slack-bolt is required: pip install
  hivepilot[slack]`. Both packages ship pure-Python wheels — safe to
  install on musl/Alpine, no build toolchain needed.
- **Discord**: `pip install hivepilot[discord]` (pulls in `discord.py` +
  `PyNaCl`). Missing it fails with `discord.py required: pip install
  hivepilot[discord]` or `PyNaCl required: pip install hivepilot[discord]`.
  `discord.py` is pure-Python, but `PyNaCl` is a **compiled** binding to
  libsodium (NOT pure Python) — it does publish prebuilt `musllinux` wheels
  for `x86_64`/`aarch64` on PyPI, so a plain `pip install` still avoids a
  from-source build (and the `libsodium-dev` + compiler it would otherwise
  need) on those architectures.

## Install

```bash
# Pick the services you need — hivepilot-api and hivepilot-scheduler are
# mandatory; hivepilot-telegram/hivepilot-slack/hivepilot-discord are each
# optional and independent of one another.
cp deploy/openrc/init.d/hivepilot-api      /etc/init.d/hivepilot-api
cp deploy/openrc/init.d/hivepilot-scheduler /etc/init.d/hivepilot-scheduler
cp deploy/openrc/init.d/hivepilot-slack     /etc/init.d/hivepilot-slack
cp deploy/openrc/init.d/hivepilot-discord   /etc/init.d/hivepilot-discord
chmod +x /etc/init.d/hivepilot-*

cp deploy/openrc/conf.d/hivepilot-api.example      /etc/conf.d/hivepilot-api
cp deploy/openrc/conf.d/hivepilot-scheduler.example /etc/conf.d/hivepilot-scheduler
cp deploy/openrc/conf.d/hivepilot-slack.example     /etc/conf.d/hivepilot-slack
cp deploy/openrc/conf.d/hivepilot-discord.example   /etc/conf.d/hivepilot-discord
# Edit each /etc/conf.d/hivepilot-* file: fill in real tokens/URLs.
chmod 600 /etc/conf.d/hivepilot-*   # these now hold secrets

rc-update add hivepilot-api default
rc-update add hivepilot-scheduler default
rc-update add hivepilot-slack default
rc-update add hivepilot-discord default

rc-service hivepilot-api start
rc-service hivepilot-scheduler start
rc-service hivepilot-slack start
rc-service hivepilot-discord start
```

Check status / logs:

```bash
rc-service hivepilot-slack status
tail -f /var/log/hivepilot-slack.log
```

## Env-silo warning (cwd=/)

**OpenRC services run with `cwd=/`** — there is no working directory
inherited from your shell, your checkout, or the `conf.d` file's own
location. Any relative path referenced by a config value (a config-repo
clone path, a plugin dir, a font path, `obsidian_vault`, etc.) resolves
against `/`, not against where you'd expect. Every path-shaped value in the
`conf.d/*.example` files in this directory is already absolute
(`/data`, `/opt/hivepilot/venv/...`) — keep it that way when you add your
own. This has bitten real deployments before (duplicate state from a
config value that silently resolved to `/` instead of `/data`) — when in
doubt, use an absolute path.

## Env silos — read this

Each service above is a **separate OpenRC process with its own, independent
`/etc/conf.d/hivepilot-<svc>` env file** — there is no environment shared
across `hivepilot-api`, `hivepilot-scheduler`, `hivepilot-telegram`,
`hivepilot-slack`, and `hivepilot-discord`. Setting a variable in one
service's `conf.d` file has **zero effect** on any other service.

This matters more than it looks like, because **an approval callback
(Telegram/Slack/Discord) resumes the pipeline INSIDE that bot's own
process** — not inside `hivepilot-scheduler`, which only kicked the run off.
A concrete incident: `IS_SANDBOX=1` was set in `hivepilot-api`'s and
`hivepilot-scheduler`'s `conf.d` files (2 of 3 relevant services) but not
`hivepilot-telegram`'s. Runs that reached a checkpoint and got resumed via a
Telegram approval executed the developer stage **without** `IS_SANDBOX`,
and it failed — the var was invisible in the process that actually resumed
the pipeline, even though it "looked" configured everywhere that mattered
at a glance.

**Remedy:** put every var that must apply to *every* service — anything a
resumed pipeline might need, plus any other cross-cutting setting — in one
file, `/etc/hivepilot/shared.env` (see
[`conf.d/shared.env.example`](conf.d/shared.env.example) for the vars that
belong there), and source it from the first line of every
`/etc/conf.d/hivepilot-*` file (already done in every `conf.d/*.example`
template in this directory):

```sh
[ -f /etc/hivepilot/shared.env ] && . /etc/hivepilot/shared.env
```

Service-specific `conf.d` files stay for values that genuinely differ per
service (tokens, ports, allow-lists); `shared.env` is only for values that
must be identical everywhere.

## Restart requirements

Editing a `conf.d/hivepilot-*` file only takes effect on the next
`rc-service <svc> restart` (OpenRC does not hot-reload `conf.d`). See
[DEPLOY-PRODUCTION.md's "Config hot-reload" section](../../docs/DEPLOY-PRODUCTION.md#9-operations)
for which *application-level* config changes (`roles.yaml`/`projects.yaml`/
etc., synced via `hivepilot config sync`) do NOT need a daemon restart —
those are reloaded live via `hivepilot reload` / `SIGHUP` instead. A change
to any environment variable in these `conf.d` files (a new token, a
different allow-list) always needs a restart of the affected service, since
environment variables are only read once, at process start.

## Troubleshooting

### `supervise-daemon: not found` / `eend: not found`

**Symptom** — `rc-service hivepilot-<svc> start` fails, and the log (or
console output) shows:

```
/usr/libexec/rc/sh/openrc-run.sh: eval: line 32: supervise-daemon: not found
/usr/libexec/rc/sh/openrc-run.sh: line 113: eend: not found
 * ERROR: hivepilot-<svc> failed to start
```

The `eend: not found` line is a **red herring** — it's just openrc-run
itself failing to report the error because its own helpers went missing
once the shell that ran `supervise-daemon` had a broken `PATH`.

**Cause** — a `conf.d/hivepilot-<svc>` file **replaced** `PATH` instead of
prefixing it, e.g. `export PATH="/opt/hivepilot/venv/bin"` (bare, no
`$PATH`). This drops the inherited `PATH`, which includes `/sbin` — and
`supervise-daemon` lives at `/sbin/supervise-daemon`. With `/sbin` gone,
openrc-run can no longer find it, and the service can't start.

**Fix** — always prefix, never replace, e.g.:

```sh
export PATH="/root/.local/bin:/opt/hivepilot/venv/bin:$PATH"
```

Every `conf.d/hivepilot-*.example` template in this directory already does
this correctly — if you hand-edited a `conf.d` file's `PATH` line, restore
the `:$PATH` suffix.

### Clearing a wedged service after a failed start

A service that failed to start (e.g. from the `PATH` bug above) can leave
OpenRC believing it's still starting/stopping, so a plain `rc-service
hivepilot-<svc> start` no-ops or errors again. Clear the stuck state first:

```sh
rc-service hivepilot-<svc> zap
rc-service hivepilot-<svc> start
```
