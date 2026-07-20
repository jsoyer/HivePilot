# Production Deployment Runbook (Alpine Linux)

A fresh-Alpine → running-production-system walkthrough. Covers both install paths
(container and bare-metal), wiring your private config repo, secrets, the
pre-production green-gate checks, installing agent CLIs, and day-2 operations.

For the conceptual overview (systemd units, Kubernetes/Helm, multi-tenancy,
observability) see [DEPLOYMENT.md](DEPLOYMENT.md). This doc is the
Alpine-specific, copy-pasteable path — including OpenRC service scripts, since
Alpine has no systemd.

## Quick-start (fresh Alpine → running, container path)

The minimal sequence, assuming you already have a private config repo and a
deploy key/token for it:

```bash
git clone https://github.com/jsoyer/HivePilot.git && cd HivePilot
cp .env.example .env
# edit .env: set HIVEPILOT_CONFIG_REPO=<your-config-repo> and any secrets you need
docker compose up -d --build
docker compose exec hivepilot hivepilot config sync
docker compose exec hivepilot hivepilot validate --dir /data
docker compose exec hivepilot hivepilot doctor

# Bootstrap API tokens — the scheduler container REQUIRES one to start (it will
# restart-loop with "Token required..." until this is done; expected, see step 7).
docker compose exec hivepilot hivepilot tokens add --role admin --note bootstrap
#   -> New token (admin): <ADMIN_TOKEN>   (save it now -- shown once)
docker compose exec hivepilot hivepilot tokens add --role run --note scheduler --token <ADMIN_TOKEN>
#   -> New token (run): <RUN_TOKEN>       (save it now -- shown once)
echo "HIVEPILOT_API_TOKEN=<RUN_TOKEN>" >> .env
docker compose up -d   # recreate the scheduler container so it picks up the token

curl -s http://127.0.0.1:8045/health
```

If `validate`/`doctor` come back clean, the scheduler container stays up (no
more restart-loop), and `/health` returns `{"status": "ok", ...}`, you have a
running API + scheduler. Continue to [Install the agent CLI(s)](#6-install-the-agent-cli-s)
before triggering a real pipeline run.

## 1. Prerequisites

- A fresh Alpine Linux host (container base image or bare metal — both covered below).
- **A private config repo** — your own `projects.yaml` / `tasks.yaml` / `roles.yaml` /
  `pipelines.yaml` / `policies.yaml` / `groups.yaml` / `schedules.yaml` /
  `model_profiles.yaml` / `prompts/` tree, in its own git repository. This is
  **not** part of the public HivePilot repo — see [Wire the config](#3-wire-the-config).
- Git access to that config repo (an SSH deploy key or a token embedded in the
  clone URL — never commit either into HivePilot's own repo or `.env`-in-image).
- At least one agent CLI you plan to run pipelines with (e.g. Claude Code) — see
  [Install the agent CLI(s)](#6-install-the-agent-cli-s). `doctor` and `validate`
  both work without one installed; only real pipeline runs need it.

## 2. Two install paths

### A — Docker/Podman Compose (recommended for production)

The image and `docker-compose.yml` in this repo already define the production
topology: an `hivepilot` service (API) and a `scheduler` service, sharing one
named volume for `/data` (synced config + `state.db`).

```bash
cp .env.example .env
# edit HIVEPILOT_CONFIG_REPO and any secrets in .env
docker compose up -d --build
```

This starts two containers from the same image (`command:` overrides which
process each one runs — see `docker-compose.yml`):

- `hivepilot` → `hivepilot api serve --host 0.0.0.0 --port 8045`
- `scheduler` → `hivepilot schedule daemon --interval 30`

The API port binds to **`127.0.0.1:8045` on the host by default** — loopback
only. `hivepilot api serve` itself does not require a token to start (unlike
the scheduler — see [7. API tokens](#7-api-tokens-bootstrap)), but its
authenticated endpoints check bearer tokens from `api_tokens.yaml` /
`HIVEPILOT_API_TOKEN`, so **put a reverse proxy (TLS + auth) in front before
exposing this beyond loopback.** HivePilot ships a Caddy integration for this
(`hivepilot caddy generate` / `setup` / `reload` — see
[DEPLOYMENT.md#reverse-proxy-caddy](DEPLOYMENT.md#reverse-proxy-caddy)); any
other TLS-terminating proxy works too.

**The `scheduler` service in `docker-compose.yml` will not start without
`HIVEPILOT_API_TOKEN` set in `.env`** (it runs `schedule daemon`, which is
fail-closed on auth — see [7. API tokens](#7-api-tokens-bootstrap)). Bootstrap
the token first, then add `HIVEPILOT_API_TOKEN=<run-token>` to `.env` before
(or bring the scheduler back up after) `docker compose up -d`.

Podman note: the image's `HEALTHCHECK` instruction (used if you `podman run`/
`podman build` the image directly, outside compose) is a Docker-format
instruction — Podman ignores it unless you build/run with
`--format docker`. The `healthcheck:` block inside `docker-compose.yml` itself
is unaffected either way (`docker compose` re-declares it explicitly).

Makefile shortcuts (equivalent to the raw compose commands above):

```bash
make docker-build    # docker compose build hivepilot
make docker-run       # docker compose up -d hivepilot scheduler
make docker-doctor    # docker compose run --rm dev hivepilot doctor
```

### B — Bare-metal via `scripts/install-alpine.sh`

```bash
apk add --no-cache git curl bash ca-certificates   # the installer also does this
sh scripts/install-alpine.sh
```

No compiler toolchain is required (see the script's own header comment — all
compiled Python deps resolve to musllinux wheels). By default this installs
from `git+https://github.com/jsoyer/HivePilot.git@main` into a fresh venv at
`/opt/hivepilot/venv`, symlinking `hivepilot` onto `/usr/local/bin`. Pin a
release instead of trailing `main` in production:

```bash
HIVEPILOT_REPO_REF=v0.2.0 sh scripts/install-alpine.sh
```

Create a dedicated non-root user and data directory (the container image does
this automatically; a bare-metal install does not):

```bash
addgroup -S hivepilot
adduser -S -G hivepilot -h /data hivepilot
mkdir -p /data
chown -R hivepilot:hivepilot /data
```

**Run the API and scheduler as OpenRC services** — Alpine's init system is
OpenRC, not systemd, so `hivepilot api systemd-unit` / `hivepilot schedule
systemd-unit` do not apply here. There is no built-in OpenRC generator; use the
scripts below as a starting point.

`/etc/conf.d/hivepilot-api` (env shared by the API's OpenRC script):

```sh
export HIVEPILOT_BASE_DIR=/data
export HIVEPILOT_API_HOST=127.0.0.1
export HIVEPILOT_API_PORT=8045
export HIVEPILOT_CONFIG_REPO=<your-config-repo>
# HIVEPILOT_API_TOKEN is NOT required for `api serve` to start (unlike the
# scheduler below) -- only set it here if some other admin-role CLI command
# you run against this host needs one. See step 7.
```

`/etc/init.d/hivepilot-api`:

```sh
#!/sbin/openrc-run

name="hivepilot-api"
description="HivePilot HTTP API server"
command="/usr/local/bin/hivepilot"
command_args="api serve --host ${HIVEPILOT_API_HOST:-127.0.0.1} --port ${HIVEPILOT_API_PORT:-8045}"
command_user="hivepilot:hivepilot"
command_background="yes"
pidfile="/run/${RC_SVCNAME}.pid"
output_log="/var/log/hivepilot/${RC_SVCNAME}.log"
error_log="/var/log/hivepilot/${RC_SVCNAME}.log"
directory="/data"

depend() {
    need net
    after firewall
}
```

`/etc/conf.d/hivepilot-scheduler` — same as above, minus the `API_HOST`/`API_PORT`
lines, **plus a run-role `HIVEPILOT_API_TOKEN`** (the scheduler daemon is
fail-closed and refuses to start without one — see
[7. API tokens](#7-api-tokens-bootstrap) for how to mint it):

```sh
export HIVEPILOT_BASE_DIR=/data
export HIVEPILOT_CONFIG_REPO=<your-config-repo>
export HIVEPILOT_API_TOKEN=<run-token>
```

`/etc/init.d/hivepilot-scheduler`:

```sh
#!/sbin/openrc-run

name="hivepilot-scheduler"
description="HivePilot scheduler daemon"
command="/usr/local/bin/hivepilot"
command_args="schedule daemon --interval 30"
command_user="hivepilot:hivepilot"
command_background="yes"
pidfile="/run/${RC_SVCNAME}.pid"
output_log="/var/log/hivepilot/${RC_SVCNAME}.log"
error_log="/var/log/hivepilot/${RC_SVCNAME}.log"
directory="/data"

depend() {
    need net
    after firewall
}
```

Before starting the scheduler, bootstrap the `<run-token>` referenced in its
`conf.d` file above — see [7. API tokens](#7-api-tokens-bootstrap) for the
exact commands. `hivepilot tokens add` works directly against local state
(`state.db`), so it can be run from the installed CLI before any service is
started: `HIVEPILOT_BASE_DIR=/data /usr/local/bin/hivepilot tokens add --role admin ...`.

Enable and start both:

```bash
mkdir -p /var/log/hivepilot && chown hivepilot:hivepilot /var/log/hivepilot
chmod +x /etc/init.d/hivepilot-api /etc/init.d/hivepilot-scheduler
rc-update add hivepilot-api default
rc-update add hivepilot-scheduler default
rc-service hivepilot-api start
rc-service hivepilot-scheduler start
```

## 3. Wire the config

Point HivePilot at your private config repo and pull it down:

```bash
export HIVEPILOT_CONFIG_REPO=<your-config-repo>   # e.g. git@github.com:you/hivepilot-config.git
hivepilot config sync
hivepilot config status
```

For a private repo, authenticate via one of:

- **SSH deploy key** (recommended for `git@`/`ssh://` URLs) — put the key on
  the host / mount it into the container and use an `ssh://` or `git@` URL;
  the key's own passphrase-less agent setup is outside HivePilot's scope.
- **`HIVEPILOT_CONFIG_TOKEN`** (recommended for `https://` URLs) — a
  fine-grained (or classic) GitHub PAT scoped to `Contents: read` on the
  config repo (add `write` too if you use `hivepilot config push`). Set
  `HIVEPILOT_CONFIG_REPO=https://github.com/you/hivepilot-config.git` (no
  token in the URL) plus `HIVEPILOT_CONFIG_TOKEN=<pat>`. `config_service`
  injects it as a **transient**, per-invocation `http.extraheader`
  (`Authorization: Basic base64(x-access-token:<token>)`) via `GIT_CONFIG_*`
  environment variables — it is **never** written to `.git/config`, **never**
  embedded in the repo URL, and never logged. `ssh://`/`git@` repo URLs
  ignore `HIVEPILOT_CONFIG_TOKEN` entirely (SSH auth uses the deploy key
  above instead).

**Never hardcode the token in a committed file** — set `HIVEPILOT_CONFIG_REPO`
and `HIVEPILOT_CONFIG_TOKEN` via `.env` (mounted, not baked into the image) or
the shell environment only.

`config sync` clones the repo into `~/.local/share/hivepilot/config-repo`
(`$XDG_DATA_HOME/hivepilot/config-repo`) and copies the managed files
(`projects.yaml`, `tasks.yaml`, `pipelines.yaml`, `policies.yaml`,
`schedules.yaml`, `roles.yaml`, `groups.yaml`, `model_profiles.yaml`, and the
`prompts/` directory) into `base_dir` (`/data` in the container image).

**Prompt resolution note:** role/agent prompts resolve through a fallback
chain — `$XDG_CONFIG_HOME/hivepilot/prompts/agents/<file>` → `config_repo/prompts/agents/<file>`
→ `base_dir/prompts/agents/<file>` → the **packaged default prompts** shipped
with HivePilot itself (final fallback, always present). Your config repo
*should* include `prompts/agents/*.md` if you want custom prompts, but it does
not have to — a config repo with no `prompts/` directory at all still works:
every role falls back to the packaged defaults. The Docker image seeds the
packaged copy at build time (see the Dockerfile's builder stage); a bare
`sh scripts/install-alpine.sh` install seeds it via `pip install` into the
venv's site-packages the same way.

## 4. Secrets

Never commit secrets. Set them via environment variables (`.env`, mounted —
not baked into the image) or a secrets backend plugin (Infisical, 1Password,
Bitwarden, Vaultwarden), referenced from config as `${secret:NAME}`.

`secrets_fail_mode` (a `policies.yaml` field, default `closed`): a missing or
errored secret reference **aborts the run** rather than silently falling back
to an empty or literal value. This is the safe default — keep it `closed` in
production. Resolved secret values are masked wherever output is captured
(logs, the state DB, notifier sinks).

See [SECURITY.md#secrets-management](SECURITY.md#secrets-management) for the
full model.

## 5. Green-gate checks (must pass before production)

Run both before pointing real traffic or schedules at this deployment:

```bash
hivepilot validate --dir /data     # cross-reference validation (roles/tasks/pipelines/policies)
hivepilot doctor                    # paths, binaries, agent CLI availability
```

`validate` prints `OK` and exits 0 when clean; any cross-reference problem
(e.g. a pipeline stage referencing an undefined role) is printed as
`ERROR  <problem>` and exits 1 — fix the offending config file before
proceeding.

`doctor` prints several sections; the important ones for a fresh deploy:

- `=== External binaries ===` — `git`/`gh`/`caddy` found on `PATH` or not.
- `=== Mandatory agent CLIs ===` — at least one of `claude`/`codex`/`vibe` must
  show `found`; otherwise the verdict line reads
  `FAIL (none of claude/codex/vibe found -- run 'hivepilot init' for details)`.
  A `NOT FOUND` line for an agent CLI you don't intend to use is expected and
  fine — resolve it only if a role in your config actually depends on that
  runner (see [Install the agent CLI(s)](#6-install-the-agent-cli-s) below).
- `=== Config repo ===` — confirms `HIVEPILOT_CONFIG_REPO` resolved correctly.

A clean run looks like `doctor`'s binaries/agents sections all showing `found`
(or an expected, non-blocking `NOT FOUND` for a runner you don't use) and
`validate` printing `OK`.

## 6. Install the agent CLI(s)

```bash
hivepilot agents list
```

Lists every known agent-CLI kind, whether its binary is on `PATH`, and whether
a guided install is available. Then, for a supported kind (e.g. `claude`):

```bash
hivepilot agents install claude
```

This is **confirm-then-run and interactive only** — it prompts for a y/N
confirmation (skip the prompt with `-y`/`--yes`, but it still refuses to run
outside a real TTY regardless of that flag) before executing the vendor's
official installer. It never runs unattended in a non-interactive context
(CI, cron, a headless pipeline step) — the operator must be present to
consent.

For `gh` (GitHub CLI), there is no scripted installer in HivePilot — install
it via Alpine's package manager (`apk add github-cli`, when available for your
Alpine release/arch) or see
[docs.github.com/en/github-cli](https://docs.github.com/en/github-cli/github-cli/quickstart).
`gh` is optional — HivePilot degrades gracefully without it; `doctor` reports
its absence under `=== External binaries ===`.

## 7. API tokens (bootstrap)

**Do this before starting the scheduler daemon** (step 2A's `scheduler`
service or step 2B's `hivepilot-scheduler` OpenRC service) — it is
fail-closed and will not run without a token.

**The first token created on a fresh system MUST be `admin`.** Any other
first role is rejected:

```bash
hivepilot tokens add --role run --note "scheduler"
```

```
Invalid value: First token must be admin
```

Bootstrap order — create the admin token first, then use it to authorize
minting a lesser-role token for the scheduler (every token mint after the
first requires an existing admin token, passed via `--token`):

```bash
hivepilot tokens add --role admin --note "bootstrap"
```

```
New token (admin): <ADMIN_TOKEN>
Save this token now -- it will not be shown again.
```

```bash
hivepilot tokens add --role run --note "scheduler" --token <ADMIN_TOKEN>
```

```
New token (run): <RUN_TOKEN>
Save this token now -- it will not be shown again.
```

Both tokens are shown **exactly once** at creation time and stored **hashed**
(SHA-256) thereafter — `hivepilot tokens list` shows role/note/expiry, never
the raw value; there is no way to recover a lost token, only rotate
(`hivepilot tokens rotate`) or mint a new one.

**The scheduler daemon requires a `run`-role token to start at all** —
`hivepilot schedule daemon` with no token set fails immediately:

```
Invalid value: Token required. Pass --token or set HIVEPILOT_API_TOKEN.
```

Supply it via the environment (preferred for a long-running service) or
`--token`:

```bash
export HIVEPILOT_API_TOKEN=<RUN_TOKEN>
hivepilot schedule daemon --interval 30
```

```
Starting scheduler daemon (interval=30s, shutdown_timeout=120s)
```

Wire the token into the service that actually runs the daemon:

- **Compose (step 2A)**: add `HIVEPILOT_API_TOKEN=<RUN_TOKEN>` to `.env` —
  `docker-compose.yml`'s `scheduler` service already loads `env_file: .env`,
  so no compose-file edit is needed. Re-run `docker compose up -d` to recreate
  the container with the new value (a plain restart does not re-read `.env`).
- **OpenRC (step 2B)**: add `export HIVEPILOT_API_TOKEN=<RUN_TOKEN>` to
  `/etc/conf.d/hivepilot-scheduler` (already shown in step 2B above) before
  `rc-service hivepilot-scheduler start`.

This same requirement extends beyond the daemon: most mutating CLI commands
(`run`, `run-pipeline`, `schedule *`, `approvals *`, `tokens *`, `debate`,
`audit`) require a role token the same way — read-only/setup commands
(`doctor`, `validate`, `config sync`/`status`, `plugins list`,
`agents list`/`install`) do not. Exporting `HIVEPILOT_API_TOKEN` once (a
`run`-role token covers `run`/`run-pipeline`; `approve`/`admin` are needed for
approvals/token management respectively) in the shell you operate from covers
all of these.

## 8. Run + verify

Start both processes (already running if you used Compose in step 2A, or the
OpenRC services in step 2B):

```bash
curl -s http://127.0.0.1:8045/health
```

Expect a JSON body like `{"status": "ok", "checks": {"database": "ok",
"runners": "ok (N defined)", ...}}` (`status` reads `degraded` only if the
state DB check itself failed). For a minimal liveness probe (no dependency
checks), use `/healthz` instead — it always returns `{"status": "ok"}` once
the process is up.

Check plugins loaded correctly:

```bash
hivepilot plugins list
hivepilot plugins health   # non-zero exit if any plugin health check fails — CI/deploy-gate friendly
```

Finally, confirm the config/validate loop and a dry pipeline preview work
end-to-end (this needs the `run`-role token from step 7 — export
`HIVEPILOT_API_TOKEN` as shown there, or pass `--token <RUN_TOKEN>`):

```bash
hivepilot config sync
hivepilot validate --dir /data
hivepilot run-pipeline <your-pipeline> --project <your-project>   # safe: defaults to --dry-run
```

## 9. Operations

- **State**: `state.db` (SQLite) lives under `base_dir` (`/data` in the
  container image) alongside the synced config. Persist this — it holds run
  history, approvals, and tokens. In Compose, the named volume `hivepilot-data`
  already covers this; in the bare-metal/OpenRC path, back up `/data`
  directly.
- **Non-root**: the container image runs as the unprivileged `hivepilot` user;
  the OpenRC scripts above run as `hivepilot:hivepilot` for the same reason.
- **Logs**: structured JSON to stdout/stderr by default (captured by
  `docker compose logs` in the container path) or to the `output_log`/
  `error_log` paths configured in the OpenRC scripts above.
- **Healthcheck**: `hivepilot doctor` doubles as the container `HEALTHCHECK`
  (see the Dockerfile) — a non-zero exit means a hard failure (missing base
  dir, a mandatory binary reported missing).
- **Updating**:
  - Config only: `hivepilot config sync` on the running host/container, THEN
    trigger a hot-reload (see "Config hot-reload" below) — `config sync`
    alone only updates the files on disk; a running `api serve` or `schedule
    daemon` process keeps its already-loaded roles/projects/tasks/pipelines
    in memory until reloaded.
  - The application itself: rebuild/re-pull the image
    (`docker compose build --pull && docker compose up -d`) for the container
    path, or re-run `HIVEPILOT_REPO_REF=<new-tag> sh scripts/install-alpine.sh`
    for bare metal (idempotent — reuses the existing venv).
- **Backup**: back up `state.db` and your config repo (the source of truth for
  `projects.yaml`/`tasks.yaml`/etc. — `/data`'s copy is a synced mirror, not
  the canonical copy).
- **Config hot-reload** (Phase 14c, #249): `config sync`'d changes to
  `roles.yaml`/`projects.yaml`/`tasks.yaml`/`pipelines.yaml` now take effect
  in a running process WITHOUT a restart, via three equivalent paths:
  - `hivepilot reload --token <admin-token>` (CLI, calls the API below)
  - `POST /v1/admin/reload` (admin-role token required) against a running
    `api serve` process — reloads roles AND projects/tasks/pipelines
  - Sending a running `schedule daemon` process `SIGHUP` — reloads roles
    unconditionally; projects/tasks/pipelines are already re-read fresh on
    every scheduler tick, so there is nothing else to reload there. An
    opt-in `HIVEPILOT_CONFIG_HOT_RELOAD=true` also reloads roles
    automatically on every tick (default off).

  **Fail-closed-to-previous guarantee**: a reload never leaves a process
  without a working config. If the new `roles.yaml` (or
  `projects.yaml`/`tasks.yaml`/`pipelines.yaml`) is missing, unparseable, or
  fails validation, the reload is rejected and the process KEEPS the
  configuration it already had loaded — it is never silently downgraded to
  a built-in default roster. `hivepilot reload` / `POST /v1/admin/reload`
  report this per-part (`roles_reloaded`/`config_reloaded` booleans); check
  the `api serve`/`schedule daemon` process logs for the underlying load
  error when either is `false`.

  **Known residual**: a reload landing MID pipeline run could let a LATER
  stage of that same run see the new role/task/pipeline bindings while an
  earlier stage of it already ran against the old ones — roles and
  orchestrator config are read live at each call site, not snapshotted
  per-run. Reloading between runs (the SIGHUP / tick-boundary / explicit
  admin-endpoint call patterns above) avoids this in practice; eliminating
  it fully would require threading a per-run config snapshot through
  `run_task`/`run_pipeline` — not implemented.

## See also

- [DEPLOYMENT.md](DEPLOYMENT.md) — systemd units (non-Alpine hosts), Kubernetes/Helm, multi-tenancy, observability
- [CONFIGURATION.md](CONFIGURATION.md) — full config resolution chain and file reference
- [SECURITY.md](SECURITY.md) — approval gates, secrets model, fail-closed policies
- [CLI-REFERENCE.md](CLI-REFERENCE.md) — every command, including mutating/destructive markers
- [PLUGINS.md](PLUGINS.md) — secrets-backend plugins (Infisical, 1Password, Bitwarden, Vaultwarden)
