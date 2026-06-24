# HivePilot Operator Runbook

> Production readiness guide for operators deploying and running HivePilot.
> All commands and settings are grounded in the actual source code — nothing is invented.
> The example deployment referenced throughout is **noxys** (the default config shipped in the repo).

---

## 1. Overview

HivePilot is a **YAML-driven multi-agent orchestrator** that runs a configurable company of role-bound AI agents against one or many repositories. A single CLI entry point (`hivepilot`) drives planning, execution, approval gates, scheduling, and remote control.

**Engine vs config split:**

| Layer | What it is | Examples |
|---|---|---|
| **Engine** | Python package (`hivepilot/`) — never edited per deployment | orchestrator, runners, API, scheduler, token service |
| **Config** | YAML files + `.env` in your working directory | `projects.yaml`, `roles.yaml`, `policies.yaml`, `groups.yaml`, `pipelines.yaml`, `tasks.yaml` |
| **Example deployment** | The `noxys` config bundled in the repo | `groups.yaml` defines the `noxys` group; `pipelines.yaml` defines `noxys-v2` |

Swap the YAML files to orchestrate a completely different project or agent company. The engine code is untouched.

---

## 2. Install

**Python requirement:** `>=3.10` (Python 3.11 used in the official Docker image).

### Bare-metal / venv

```bash
python -m venv .venv
source .venv/bin/activate

# Core only (no agent extras, no API server)
pip install -e .

# Recommended production install (API server + Telegram bot)
pip install -e ".[api,notifications]"

# Full install (all extras — large, pulls torch)
pip install -e ".[full]"

# Dev/test install (adds pytest, ruff, mypy, bandit)
pip install -e ".[dev,notifications]"
```

**Optional extras and what they unlock:**

| Extra | Key deps | Unlocks |
|---|---|---|
| `api` | fastapi, uvicorn, prometheus-client, itsdangerous | REST API server (`hivepilot api serve`) |
| `notifications` | python-telegram-bot | Telegram bot (`hivepilot telegram start`) |
| `langchain` | langchain, faiss-cpu, sentence-transformers, torch | RAG-based context (large download) |
| `dashboard` | textual | Textual TUI dashboard |
| `slack` | slack-bolt | Slack bot |
| `discord` | discord.py, PyNaCl | Discord bot |
| `cloud` | boto3 | AWS runner |
| `containers` | docker | Container runner |

**Optional system deps (not Python packages):**

| Dep | Purpose | Install |
|---|---|---|
| `bwrap` (bubblewrap) | Sandbox mode for developer steps | `apt install bubblewrap` |
| `psycopg[binary]` | PostgreSQL backend | `pip install "psycopg[binary]"` |
| `hvac` | HashiCorp Vault secret resolver | `pip install hvac` |

### Docker

```bash
docker build -t hivepilot:latest .
docker run --rm -v $(pwd):/app -w /app hivepilot:latest hivepilot --help
```

The Dockerfile uses `python:3.11-slim`, installs `git`, `curl`, the GitHub CLI (`gh`), then `pip install -e .` from a copied `requirements.txt`. The default CMD is `hivepilot --help`; override for production:

```bash
docker run -d --name hivepilot-api \
  --env-file .env \
  -v $(pwd):/app -w /app \
  -p 8045:8045 \
  hivepilot:latest hivepilot api serve --host 0.0.0.0 --port 8045
```

### Verify install

```bash
hivepilot doctor   # checks paths, external binaries, optional extras, proxy settings
```

---

## 3. Configure

### Config file layout

All paths are relative to `HIVEPILOT_BASE_DIR` (default: `cwd`). XDG override: files in `~/.config/hivepilot/` take priority over the working directory.

| File | Purpose |
|---|---|
| `projects.yaml` | One entry per repository: path, branch, GitHub owner/repo, env vars |
| `roles.yaml` | Agent role definitions: runner, model, prompt file, can_block, order |
| `policies.yaml` | Per-project overrides: auto_git, require_approval, allow_containers |
| `groups.yaml` | Groups of projects (e.g. a product with many repos); defines the `hub` for group-scoped planning |
| `pipelines.yaml` | Named pipelines: ordered list of stage→task pairs; `pause_before: true` = human checkpoint |
| `tasks.yaml` | Task definitions (steps, runner, prompt) and runner registry |
| `schedules.yaml` | Cron-style schedules for recurring runs |
| `.env` | All secrets and deployment-specific settings (never commit) |
| `api_tokens.yaml` | Hashed API tokens (written by `hivepilot tokens add`; never commit) |

### Scaffold a new config directory

```bash
hivepilot init minimal --name my-project --dest ./config
# Templates: minimal, blog, iac, security
hivepilot init --list   # list available templates
```

### Key `.env` settings

All settings use the `HIVEPILOT_` prefix (from `hivepilot/config.py`):

```dotenv
# --- Core paths ---
HIVEPILOT_BASE_DIR=/path/to/config-dir      # where YAML files live
HIVEPILOT_ENV_FILE=/path/to/.env            # explicit .env override (optional)

# --- Routing ---
HIVEPILOT_DEFAULT_TARGET=noxys              # default project/group for @mention and /ask
HIVEPILOT_DEFAULT_PIPELINE=noxys-v2        # default pipeline for @mention

# --- Agent runner ---
HIVEPILOT_CLAUDE_COMMAND=claude             # path/name of the claude binary
HIVEPILOT_CLAUDE_PERMISSION_MODE=acceptEdits  # acceptEdits | bypassPermissions | plan | default
# acceptEdits: agent edits files autonomously, bash still gated
# bypassPermissions: full autonomy (required for headless dev runs)
# Omit or leave empty for read-only/planning agents

# --- Concurrency & quota ---
HIVEPILOT_CLAUDE_MAX_CONCURRENCY=1         # max concurrent claude steps
HIVEPILOT_CONCURRENCY_LIMIT=4              # overall worker concurrency
HIVEPILOT_DEV_FALLBACK_RUNNERS=codex,cursor  # runners tried on claude quota exhaustion
HIVEPILOT_DEV_BATCH_SIZE=0                 # fan-out batch size (0 = unlimited)

# --- API server ---
HIVEPILOT_API_HOST=127.0.0.1
HIVEPILOT_API_PORT=8045
HIVEPILOT_API_ROOT_PATH=                   # set to /hivepilot if behind path-prefix proxy
HIVEPILOT_API_ALLOWED_ORIGINS=             # comma-separated CORS origins

# --- Database (optional Postgres) ---
HIVEPILOT_DATABASE_URL=                    # e.g. postgresql://user:pass@host/db (requires psycopg)
                                           # default: SQLite at state.db

# --- Sandbox ---
HIVEPILOT_DEV_SANDBOX=none                 # none | bwrap
HIVEPILOT_WORKTREE_ISOLATION=true          # run dev/role tasks in a throwaway git worktree

# --- Governance ---
HIVEPILOT_GOVERNANCE_REPO=/path/or/url    # shared governance repo (CLAUDE.md, AGENTS.md, etc.)

# --- Telegram bot ---
HIVEPILOT_TELEGRAM_BOT_TOKEN=             # BotFather token
HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS=[-100xxx]   # JSON array of allowed chat IDs
HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID=-100xxx  # proactive notifications
HIVEPILOT_TELEGRAM_STREAM_CHAT_ID=-100xxx        # live agent output (falls back to notification)
HIVEPILOT_TELEGRAM_WEBHOOK_URL=           # public base URL for webhook mode
HIVEPILOT_TELEGRAM_WEBHOOK_SECRET=        # optional webhook secret

# --- Config repo sync ---
HIVEPILOT_CONFIG_REPO=/path/or/https-url  # shared config repo; `hivepilot config sync` pulls it
HIVEPILOT_CONFIG_BRANCH=main

# --- Vault (optional) ---
HIVEPILOT_VAULT_ADDR=https://vault.example.com
HIVEPILOT_VAULT_TOKEN=hvs.xxxx

# --- Token TTL ---
HIVEPILOT_TOKEN_TTL_DAYS=90               # default expiry for new tokens (optional)

# --- Event webhook (n8n, etc.) ---
HIVEPILOT_EVENT_WEBHOOK_URL=https://n8n.example.com/webhook/hivepilot
HIVEPILOT_EVENT_WEBHOOK_TOKEN=            # optional Bearer token
```

### Roles (roles.yaml)

Roles bind agent persona to runner + model. Example from the bundled config:

```yaml
roles:
  - name: developer
    display_name: "Gustave"
    title: "Developer"
    command_task: "noxys-developer"
    prompt_file: "developer.md"          # relative to prompts/agents/
    model_profile: "coding"
    runner: "claude"
    permission_mode: "bypassPermissions"
    can_block: false
    order: 4
```

Per-project runner/model overrides live in `policies.yaml` under `role_overrides`.

### Validate config

```bash
hivepilot lint    # checks all YAML files for structural errors
```

---

## 4. Run the Services

HivePilot has three persistent services: the **API server**, the **scheduler daemon**, and the **Telegram bot**. Each has a `systemd-unit` command that prints a ready-to-use unit file.

### 4.1 API server

```bash
# Generate the unit file
hivepilot api systemd-unit \
  --user hivepilot \
  --working-dir /path/to/config \
  --env-file /path/to/.env \
  > /etc/systemd/system/hivepilot-api.service

sudo systemctl daemon-reload
sudo systemctl enable --now hivepilot-api
```

The API server listens on `HIVEPILOT_API_HOST:HIVEPILOT_API_PORT` (default `127.0.0.1:8045`).
Put Caddy or nginx in front for TLS. Caddy one-shot setup:

```bash
hivepilot caddy setup hivepilot.example.com --email ops@example.com
```

Start manually (for testing):
```bash
hivepilot api serve --host 127.0.0.1 --port 8045
```

> **WARNING:** Running with `--workers > 1` + SQLite will corrupt state. Use `workers=1` or switch to Postgres before scaling horizontally.

### 4.2 Scheduler daemon

**The scheduler daemon must be running** for:
- Quota auto-resume (deferred steps are retried at quota reset)
- Scheduled pipeline runs (`schedules.yaml`)
- Retry queue processing

```bash
# Generate the unit file
hivepilot schedule systemd-unit \
  --user hivepilot \
  --working-dir /path/to/config \
  --env-file /path/to/.env \
  --interval 30 \
  > /etc/systemd/system/hivepilot-scheduler.service

sudo systemctl daemon-reload
sudo systemctl enable --now hivepilot-scheduler
```

Start manually:
```bash
hivepilot schedule daemon --interval 30   # polls every 30s
```

Check status:
```bash
hivepilot schedule health   # shows schedule next-run times and retry queue depth
```

### 4.3 Telegram bot

The Telegram bot is a user-level service (no `sudo`). It blocks, so run it in its own unit:

```bash
# Generate the unit file (user service)
hivepilot telegram systemd-unit \
  --working-dir /path/to/config \
  --env-file /path/to/.env \
  > ~/.config/systemd/user/hivepilot-telegram.service

systemctl --user daemon-reload
systemctl --user enable --now hivepilot-telegram
loginctl enable-linger $USER   # keep running after logout

# Logs
journalctl --user -u hivepilot-telegram -f
```

**Webhook vs polling:** default is polling. For webhook mode (lower latency, no long-poll):

```bash
hivepilot telegram set-webhook https://hivepilot.example.com
hivepilot telegram start --mode webhook --webhook-url https://hivepilot.example.com
```

Find your chat ID:
```bash
hivepilot telegram chat-id   # send a message to the bot first, then run this
```

---

## 5. Auth & Tokens

### RBAC roles

| Role | Rank | Can do |
|---|---|---|
| `read` | 0 | List projects, tasks, runs, schedules |
| `run` | 1 | Execute tasks and pipelines, list approvals |
| `approve` | 2 | Approve/deny pending runs, purge DLQ |
| `admin` | 3 | All of the above + manage tokens (cross-tenant) |

### Create tokens

```bash
# Bootstrap: first token must be admin (no existing tokens file)
hivepilot tokens add --role admin --note "bootstrap"

# Add more tokens (requires an existing admin token)
hivepilot tokens add --role run    --note "CI pipeline"   --token <admin-token>
hivepilot tokens add --role read   --note "monitoring"    --token <admin-token>
hivepilot tokens add --role approve --note "on-call lead" --token <admin-token>

# With TTL (days) and tenant
hivepilot tokens add --role run --tenant acme --ttl 90 --note "acme CI" --token <admin-token>
```

> **The plaintext token is shown exactly once.** Save it immediately — it is SHA-256 hashed at rest in `api_tokens.yaml`.

### List and manage tokens

```bash
hivepilot tokens list    --token <admin-token>   # masked display
hivepilot tokens rotate  <old-token> --token <admin-token>   # new token, old invalidated
hivepilot tokens remove  <token>     --token <admin-token>
```

### Use tokens

CLI flag: `--token <value>` or env var `HIVEPILOT_API_TOKEN`.

API header: `Authorization: Bearer <token>`

---

## 6. Secrets

HivePilot supports four secret resolver backends, checked in priority order:

| Backend | How to configure | Notes |
|---|---|---|
| **Env vars** | Standard shell env or `.env` file | Default; simplest for single-host |
| **File** | Set `HIVEPILOT_SECRETS_ALLOWED_DIRS` to paths; reference `file:/path/to/secret` in config | Operator-managed files |
| **HashiCorp Vault** | `HIVEPILOT_VAULT_ADDR` + `HIVEPILOT_VAULT_TOKEN` (needs `hvac`) | Best for multi-host / team secrets |
| **SOPS** | Reference SOPS-encrypted files in config | Requires `sops` binary on PATH |

**Never commit:**
- `.env` — add to `.gitignore`
- `api_tokens.yaml` — add to `.gitignore`

**Rotation procedure:**
1. Create replacement token/secret
2. Update `.env` or Vault
3. Restart services: `systemctl restart hivepilot-api hivepilot-scheduler`
4. For tokens: `hivepilot tokens rotate <old-token> --token <admin-token>`

---

## 7. Pipelines & Approvals

### Run a pipeline

```bash
# Single project
hivepilot run-pipeline <project> <pipeline>

# Group (plans in hub, fans out to all components)
hivepilot run-pipeline noxys noxys-v2

# With options
hivepilot run-pipeline noxys noxys-v2 \
  --extra-prompt "Focus on auth subsystem" \
  --auto-git \
  --no-dry-run            # --dry-run is the default; use --no-dry-run for vault writes

# Preview (no real agent calls)
hivepilot run-pipeline noxys noxys-v2 --simulate

# Limit fan-out (useful when HIVEPILOT_DEV_BATCH_SIZE=0)
hivepilot run-pipeline noxys noxys-v2 --concurrency 3
```

### Group mode

When `<project>` matches a key in `groups.yaml`, HivePilot:
1. Runs the planning stages on the **hub** project (e.g. `noxys`)
2. Fans out execution stages to all **components** listed in the group

```yaml
# groups.yaml example
groups:
  noxys:
    hub: noxys
    components: [noxys-api, noxys-console, ...]
```

### Plan checkpoint (CHECKPOINT / `pause_before`)

Pipelines with `pause_before: true` on a stage (e.g. `Implementation` in `noxys-v2`) pause there and wait for human approval before proceeding. The run state is `pending_approval`.

**Approve via CLI:**
```bash
hivepilot approvals list   --token <token>
hivepilot approvals approve <run_id> --approver "alice" --token <token>
hivepilot approvals deny   <run_id> --reason "need more context" --token <token>
```

**Approve via API:**
```bash
curl -X POST http://localhost:8045/approvals/<run_id> \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"approve": true, "approver": "alice"}'
```

**Approve via Telegram:**
```
/approve <run_id>
```

### @mentions and aliases

From Telegram or any chat-ops integration:

| Command | Effect |
|---|---|
| `@noxys` or `@noxys-api` | Route to group or project; runs `HIVEPILOT_DEFAULT_PIPELINE` |
| `/ask <question>` | Direct question to the default target |
| `/runpipeline <project> <pipeline>` | Start a pipeline |
| `/steps` | List pipeline stages of last run |
| `/approve <run_id>` | Approve a pending checkpoint |

---

## 8. Quota Resilience

### Throttle

```dotenv
HIVEPILOT_CLAUDE_MAX_CONCURRENCY=1   # max concurrent claude invocations (default: 1)
```

Raise to 2–4 if your API plan permits parallel calls. Keep at 1 on free/low-tier plans.

### Fallback runners

When Claude hits a quota error, HivePilot tries the `dev_fallback_runners` in order:

```dotenv
HIVEPILOT_DEV_FALLBACK_RUNNERS=codex,cursor   # tried in order on quota exhaustion
```

Codex and Cursor must be on PATH. Verify with `hivepilot doctor`.

### Auto-resume (deferred steps)

Steps that hit quota are set to `deferred` state. The **scheduler daemon** picks them up and re-runs them at the next quota reset. Without the scheduler running, deferred steps never resume.

Check deferred queue:
```bash
hivepilot schedule health           # shows retry queue depth
hivepilot schedule retry-list       # detailed list
hivepilot schedule retry-list --status pending
hivepilot schedule dlq-list         # permanently failed (dead-letter)
```

### Recovering a quota-stalled run

1. Check the retry queue: `hivepilot schedule retry-list`
2. Ensure the scheduler daemon is running: `systemctl status hivepilot-scheduler`
3. If the run is in the dead-letter queue (`hivepilot schedule dlq-list`): fix the underlying issue, then purge and re-run:
   ```bash
   hivepilot schedule dlq-purge --yes --token <token>
   hivepilot run-pipeline <project> <pipeline> --token <token>
   ```

### Batching (large groups)

For groups with many components, limit how many are processed per fan-out pass:

```dotenv
HIVEPILOT_DEV_BATCH_SIZE=5   # 0 = unlimited (default)
```

This reduces concurrent Anthropic API calls when running wide groups.

---

## 9. Sandbox

The developer role (`claude` runner with `bypassPermissions`) has elevated autonomy. HivePilot provides optional bubblewrap confinement:

```dotenv
HIVEPILOT_DEV_SANDBOX=bwrap   # requires bubblewrap installed: apt install bubblewrap
HIVEPILOT_WORKTREE_ISOLATION=true  # run tasks in throwaway git worktrees (default: true)
```

**What `bwrap` protects:**
- Filesystem confinement: the agent subprocess can only see the project worktree, not the host filesystem
- Environment scrub: only whitelisted env vars are passed into the sandbox

**Env allowlist:**
```dotenv
HIVEPILOT_SANDBOX_ENV_ALLOWLIST=HOME,PATH,GIT_AUTHOR_NAME   # empty = use built-in defaults
```

**Fallback:** if `bwrap` is unavailable or errors, HivePilot falls back silently to unsandboxed execution with a log warning. Set `dev_sandbox=none` in CI environments where bwrap is unavailable.

**Combined:** `HIVEPILOT_WORKTREE_ISOLATION=true` + `HIVEPILOT_DEV_SANDBOX=bwrap` gives both git isolation and OS-level filesystem confinement.

---

## 10. Multi-Tenant

HivePilot supports multiple tenants on a single deployment. Tenant is attached to each API token at creation time.

```bash
hivepilot tokens add --role run --tenant acme   --note "acme CI"   --token <admin-token>
hivepilot tokens add --role run --tenant beta   --note "beta team"  --token <admin-token>
```

**Isolation:**
- Non-admin tokens see only their own tenant's runs and approvals
- Admin tokens see all tenants
- Cross-tenant approval is rejected with HTTP 403

**API behavior:**
- `GET /runs` — filtered to caller's tenant (admin: all)
- `GET /approvals` — filtered to caller's tenant (admin: all)
- `POST /approvals/<run_id>` — returns 403 if run_id belongs to a different tenant (non-admin)

---

## 11. Postgres (optional, multi-host)

SQLite (`state.db`) is the default. Switch to Postgres when running multiple API workers or deploying across hosts:

```dotenv
HIVEPILOT_DATABASE_URL=postgresql://hivepilot:secret@db-host:5432/hivepilot
```

Requires `psycopg[binary]`:
```bash
pip install "psycopg[binary]"
```

> Do **not** run `--workers > 1` with SQLite — it will corrupt state. Postgres is required for horizontal scaling.

**Setup:**
```sql
CREATE USER hivepilot WITH PASSWORD 'secret';
CREATE DATABASE hivepilot OWNER hivepilot;
```

HivePilot creates its schema on first start.

---

## 12. Observability

### Health endpoints (no auth required)

| Endpoint | Purpose | Success response |
|---|---|---|
| `GET /healthz` | Liveness probe (process alive) | `{"status": "ok"}` |
| `GET /readyz` | Readiness probe (DB + config loaded) | `{"ready": true, "checks": {"db": "ok", "config": "ok"}}` — HTTP 503 if failing |
| `GET /health` | Full health check (deps) | JSON with `checks.database`, `checks.runners`, `checks.dep:*` |

### Prometheus metrics

```
GET /metrics
```

Returns Prometheus text format (no auth required). Metric exposed:

| Metric | Type | Description |
|---|---|---|
| `run_duration_seconds` | Histogram | Duration of `/run` and `/approvals/{run_id}` API operations |

Add Python `prometheus_client` default metrics (process, GC) automatically via `prometheus-client`.

**Scrape config (Prometheus):**
```yaml
scrape_configs:
  - job_name: hivepilot
    static_configs:
      - targets: ['localhost:8045']
    metrics_path: /metrics
```

### Kubernetes probes

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8045
  initialDelaySeconds: 10
  periodSeconds: 30

readinessProbe:
  httpGet:
    path: /readyz
    port: 8045
  initialDelaySeconds: 5
  periodSeconds: 10
```

### Audit log

All authenticated API requests are logged to the state database via `state_service.record_audit()` with: token hash, role, endpoint, method, result (`authorized` / `forbidden`), tenant.

### Structured logs

HivePilot uses `structlog`. Set `HIVEPILOT_LOG_TO_FILE=true` to write logs to `runs/logs/`.

---

## 13. Troubleshooting

### Quota stalls

**Symptom:** runs stay in `deferred` state; agents not advancing.

```bash
hivepilot schedule health          # check retry queue
systemctl status hivepilot-scheduler  # confirm daemon is running
journalctl -u hivepilot-scheduler -n 50  # recent logs
```

The scheduler daemon polls every `--interval` seconds (default 30). If it is not running, deferred steps never resume. Restart it:

```bash
sudo systemctl restart hivepilot-scheduler
```

### Deferred queue inspection

```bash
hivepilot schedule retry-list                        # all items
hivepilot schedule retry-list --status pending       # waiting to retry
hivepilot schedule retry-list --status running       # currently executing
hivepilot schedule dlq-list                          # permanently failed
hivepilot schedule dlq-purge --yes --token <admin>   # clear DLQ and re-run manually
```

### Worktree isolation errors

**Symptom:** `git worktree` errors or locked worktrees after a failed run.

```bash
# In the project directory
git worktree list        # show all worktrees
git worktree prune       # remove stale entries
```

The scheduler daemon cleans up worktrees on normal completion. Prune manually after crashes.

### Common errors

| Error | Cause | Fix |
|---|---|---|
| `Token required` | Missing `--token` or `HIVEPILOT_API_TOKEN` | Pass token flag or export env var |
| `Invalid token` | Wrong or expired token | Check `hivepilot tokens list`; rotate if expired |
| `Insufficient role` | Token role too low for the operation | Use a token with a higher role |
| `Rate limit exceeded` (HTTP 429) | >20 req/60s per IP on `/run`, `/chatops/*`, `/webhook/trigger/*` | Back off; check if a runaway script is calling the API |
| `uvicorn not installed` | `[api]` extra missing | `pip install -e ".[api]"` |
| `python-telegram-bot not installed` | `[notifications]` extra missing | `pip install -e ".[notifications]"` |
| `psycopg not installed` | Postgres backend configured but lib missing | `pip install "psycopg[binary]"` |
| API returns HTTP 503 from `/readyz` | Database or config not loadable | Check `journalctl -u hivepilot-api`; verify `.env` and YAML files |
| `claude: NOT FOUND` in `hivepilot doctor` | `claude` CLI not on PATH | Install Claude Code CLI; verify PATH in systemd unit |
| `WARNING: workers > 1 with SQLite` | Scaling with SQLite backend | Set `HIVEPILOT_DATABASE_URL` to Postgres |

### Lint config errors

```bash
hivepilot lint    # exits 1 and prints specific errors on YAML structural problems
```

### Environment check

```bash
hivepilot doctor  # prints paths, binaries, optional deps, proxy settings, config repo, Telegram status
```
