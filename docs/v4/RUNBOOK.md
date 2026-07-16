# HivePilot Operator Runbook

> Production readiness guide for operators deploying and running HivePilot.
> All commands and settings are grounded in the actual source code — nothing is invented.
> The example deployment referenced throughout is **acme** (the default config shipped in the repo).

---

## 1. Overview

HivePilot is a **YAML-driven multi-agent orchestrator** that runs a configurable company of role-bound AI agents against one or many repositories. A single CLI entry point (`hivepilot`) drives planning, execution, approval gates, scheduling, and remote control.

**Engine vs config split:**

| Layer | What it is | Examples |
|---|---|---|
| **Engine** | Python package (`hivepilot/`) — never edited per deployment | orchestrator, runners, API, scheduler, token service |
| **Config** | YAML files + `.env` in your working directory | `projects.yaml`, `roles.yaml`, `policies.yaml`, `groups.yaml`, `pipelines.yaml`, `tasks.yaml` |
| **Example deployment** | The `acme` config bundled in the repo | `groups.yaml` defines the `acme` group; `pipelines.yaml` defines `default` |

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

### First-run setup: onboarding wizard & config scaffolding

**Interactive onboarding wizard** (recommended — clone an existing config repo or scaffold fresh config):

```bash
hivepilot init                    # Start interactive wizard
hivepilot init --yes              # Non-interactive: scaffold minimal config locally
hivepilot init --config-repo git@github.com:you/hivepilot-config.git  # Clone existing config
```

**Or scaffold from a template:**

```bash
hivepilot init-template minimal --name my-project --dest ./config
# Templates: minimal, blog, iac, security
hivepilot init-template --list   # list available templates
```

### Key `.env` settings

All settings use the `HIVEPILOT_` prefix (from `hivepilot/config.py`):

```dotenv
# --- Core paths ---
HIVEPILOT_BASE_DIR=/path/to/config-dir      # where YAML files live
HIVEPILOT_ENV_FILE=/path/to/.env            # explicit .env override (optional)

# --- Routing ---
HIVEPILOT_DEFAULT_TARGET=acme              # default project/group for @mention and /ask
HIVEPILOT_DEFAULT_PIPELINE=default          # default pipeline for @mention
HIVEPILOT_CONTEXT_ROUTING_MODE=full         # full | keyed -- see "Context routing" below

# --- Agent runner ---
HIVEPILOT_CLAUDE_COMMAND=claude             # path/name of the claude binary
HIVEPILOT_CLAUDE_PERMISSION_MODE=acceptEdits  # acceptEdits | bypassPermissions | plan | default
# acceptEdits: agent edits files autonomously, bash still gated
# bypassPermissions: full autonomy (required for headless dev runs)
# Omit or leave empty for read-only/planning agents
HIVEPILOT_CLAUDE_CAPTURE_USAGE=false        # opt-in: per-step token/cost/actual-model capture
# false (default): byte-identical to today -- no --output-format json, raw stdout
# true: adds --output-format json, still returns only the agent's text as step
#       output; records input/output tokens + CLI-self-reported cost + actual
#       model. Any JSON/CLI failure gracefully falls back to flag-off behaviour.

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
    command_task: "acme-developer"
    prompt_file: "developer.md"          # relative to prompts/agents/
    model_profile: "coding"
    runner: "claude"
    permission_mode: "bypassPermissions"
    inputs:                              # keys this role reads from prior stage output
      - technical_spec
      - architecture_docs
      - codebase_context
    outputs:                             # keys this role's output is filed under
      - implementation
      - test_suite
      - implementation_notes
    can_block: false
    order: 4
```

`inputs`/`outputs` are a role's declared data-flow contract: `outputs` names
the keys a producing stage's output is filed under; `inputs` names the keys a
consuming stage expects from earlier stages in the same pipeline run. They are
always declared (every bundled role has both), but they only change runtime
behaviour under `context_routing_mode="keyed"` -- see "Context routing"
below. `config validate` (see "Validate config") cross-checks `inputs`
against upstream `outputs` per pipeline and flags any that are never
produced ("dangling inputs").

`can_block: true/false` is **advisory only** -- it documents whether a role
is expected to be able to halt a run (e.g. `cto`, `reviewer`, `ciso`), but it
does not itself gate anything at runtime. The actual fail-fast/continue
behaviour for a given stage is controlled by the stage-level
`continue_on_failure` field in `pipelines.yaml` (see "Stage scoping" below),
which supersedes `can_block` as the source of truth for whether a failing
stage halts the pipeline.

Per-project runner/model overrides live in `policies.yaml` under `role_overrides`.

### Context routing (`context_routing_mode`)

`HIVEPILOT_CONTEXT_ROUTING_MODE` (`full` | `keyed`, default `full`) controls how
a stage's `prior_context` (the text handed to the next agent) is assembled from
earlier stages' output, in `hivepilot/orchestrator.py`:

- **`full` (default).** Byte-identical to pre-PRD-A2 behaviour: every stage
  receives `build_prior_context()` over ALL prior stage output
  (`prior_context_mode`: `full` | `synthesis` | `cap`), regardless of what
  its role declares in `inputs`. `inputs`/`outputs` are populated into a
  run-scoped keyed store either way, but that store is never read in this
  mode.
- **`keyed` (opt-in).** A stage whose role declares a non-empty `inputs` list
  gets its `prior_context` assembled from ONLY those input keys, pulled from
  the run-scoped keyed store populated by earlier stages' declared
  `outputs`, joined as `## <KEY>` blocks (see below) and capped with the
  same tail-truncation rule as `prior_context_mode="cap"`.

**`## <KEY>` output-section convention.** A producing stage's raw agent
output can opt into precise per-key extraction by emitting Markdown `##`
headers matching its role's `outputs` keys, e.g. a role with
`outputs: [technical_spec, adr]` writing:

```markdown
## TECHNICAL_SPEC
... spec content ...

## ADR
... ADR content ...
```

Header matching is case-insensitive and normalizes `_`/`-`/whitespace runs
to a single `_` (so `## Technical Spec`, `## technical-spec`, and
`## TECHNICAL_SPEC` all match the `technical_spec` key).

**Fallbacks (conservative by design):**
- **Coarse / whole-blob fallback** — when a producing stage's output has no
  matching `## <KEY>` section for one of its declared `outputs` keys, that
  key is filed under the run-scoped store as the entire stage output blob
  instead of a precise excerpt, so every declared output key always
  resolves to *something*.
- **Missing-key fallback** — in `keyed` mode, if a consuming stage's `inputs`
  keys are ALL missing from the run-scoped store, routing a keyed slice
  would produce an empty context (worse than no routing), so it falls back
  to the full `build_prior_context()` result instead and logs a warning
  naming the missing keys. If only SOME keys are missing, the keyed slice is
  built from whichever keys ARE present (no fallback in that case — a
  partial precise slice still beats the full context).

**Optional keyed inputs (`optional_inputs`).** A role may also declare
`optional_inputs: [key, ...]` — a SEPARATE list from `inputs`, not a marker
subset of it. In `keyed` mode, `optional_inputs` keys are routed into the
stage's context exactly like `inputs` keys when an upstream stage produced
them (the routing set is `inputs + optional_inputs`, deduplicated,
present-only), but they are **never** treated as "missing" for the
missing-key fallback above, and `config validate` (below) never flags them
as dangling even when no stage in a given pipeline produces them. Use case:
a role shared across multiple pipelines that consumes a key only some of
those pipelines' stages produce — e.g. a `design_spec` key emitted only by
a UI-focused designer stage that some pipelines skip. Declaring that key as
`optional_inputs` (rather than `inputs`) lets the role pick it up where
available without dangling in every pipeline that doesn't run the
producing stage.

### Validate config

```bash
hivepilot lint             # checks all YAML files for structural errors
hivepilot config validate  # cross-reference checks across projects/roles/tasks/pipelines/groups/policies
```

`hivepilot config validate` includes a dangling-input check: for each
pipeline, it walks stages in declared order accumulating the set of output
keys produced so far, and flags any stage whose role `inputs` references a
key not yet produced upstream in that pipeline. Severity depends on
`context_routing_mode`:

- **`full` (default)** — dangling inputs are common and mostly cosmetic
  (e.g. `developer`'s `architecture_docs`/`codebase_context` are supplied
  externally, not produced by any role) since `full` mode never actually
  routes by key. They are emitted as a Python `UserWarning`, NOT added to
  the command's problem list — `config validate` still exits `0`/`OK`.
- **`keyed`** — the same dangling inputs become hard failures in the
  returned problem list (`config validate` exits `1`), because in this mode
  a dangling input means a stage silently degrades to the missing-key
  fallback above instead of getting the data it expects.

A role's `optional_inputs` keys are exempt from this check in both modes —
they are never flagged as dangling, since their whole purpose is to be
absent in pipelines that don't run the producing stage (see "Context
routing" above).

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
hivepilot run-pipeline acme default

# With options
hivepilot run-pipeline acme default \
  --extra-prompt "Focus on auth subsystem" \
  --auto-git \
  --no-dry-run            # --dry-run is the default; use --no-dry-run for vault writes

# Preview (no real agent calls)
hivepilot run-pipeline acme default --simulate

# Limit fan-out (useful when HIVEPILOT_DEV_BATCH_SIZE=0)
hivepilot run-pipeline acme default --concurrency 3
```

### Group mode

When `<project>` matches a key in `groups.yaml`, HivePilot:
1. Runs the planning stages on the **hub** project (e.g. `acme`)
2. Fans out execution stages to all **components** listed in the group

```yaml
# groups.yaml example
groups:
  acme:
    hub: acme
    components: [acme-api, acme-web, ...]
    # tags: optional dict[str, list[str]] (default {}) — a named subset of the
    # components above. Stage `only_tags` resolves through this map. Illustrative:
    tags:
      ui: [acme-web]        # real tag names/members owned by PRD B / Noxys config
```

### `single_repo` groups (monorepos)

Set `single_repo: true` on a group to model a **monorepo** instead of a
multi-repo product: `components`/`tags` become pure **scoping labels** — they
still gate *which* stages run (via `only_components`/`only_tags`, same skip
semantics as below), but every stage that runs executes **once at `hub`**
(both git actions and task execution), never fanned out per component. This
is opt-in and defaults to `false` — every existing multi-repo group is
byte-identical unless it explicitly sets `single_repo: true`.

```yaml
# groups.yaml example — monorepo group
groups:
  acme-monorepo:
    hub: acme-monorepo        # must be a real project in projects.yaml — the
                               # single git checkout where every stage runs
    single_repo: true
    components: [ui, api]     # scoping labels only — do NOT need to exist in
    tags:                     # projects.yaml (unlike multi-repo components)
      ui: [ui]
```

A stage scoped with `only_tags: [ui]` is skipped when no `ui`-tagged
component is selected for the run, and runs (once, at `acme-monorepo`) when
one is. `hub` is required whenever `single_repo: true` — validated at
`Group` construction time (raises if missing).

### Plan checkpoint (CHECKPOINT / `pause_before`)

Pipelines with `pause_before: true` on a stage (e.g. `Implementation` in `default`) pause there and wait for human approval before proceeding. The run state is `pending_approval`.

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

### Stage scoping (`only_components` / `only_tags` / `continue_on_failure`)

A pipeline stage can be restricted to a subset of the run's touched components,
and can opt out of fail-fast. All three fields are optional and backward
compatible — a stage that sets none of them behaves exactly as before.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `only_components` | `list[str] \| None` | `None` | Restrict the stage to these component names. |
| `only_tags` | `list[str] \| None` | `None` | Restrict via tag names, resolved through the run's `Group.tags` (see Group mode). |
| `continue_on_failure` | `bool` | `false` | When `true`, a failing stage does **not** halt the run — fail-fast is suppressed and the pipeline proceeds to the next stage. `false`/absent keeps the current fail-fast behaviour. |

Two other per-stage flags predate scoping: `pause_before: bool` (human plan
checkpoint, see above) and `commits_vault: bool` (default `false` — when `true`,
the stage triggers a vault changelog commit after it executes).

**Skip semantics.** The stage's *target set* is
`set(only_components or [])` ∪ (the components resolved from `only_tags`).
The stage is **skipped iff that target set is non-empty AND disjoint from the
components the run actually touches** (`selected_components`). A stage with
neither selector always runs. A skipped stage does not invoke its task, is not
counted as a failure, and leaves prior context untouched.

**Fail-closed.** An `only_tags` value that is not defined in the run's
`Group.tags` raises a clear `ValueError` up front (at load/resolve time, before
any stage runs) — an unknown tag is never silently skipped.

```yaml
# pipelines.yaml — scoped stage example
stages:
  - name: Frontend build
    task: build-ui
    only_tags: [ui]            # resolved through groups.yaml → Group.tags
    continue_on_failure: true  # a build failure here won't abort the whole run
  - name: API deploy
    task: deploy-api
    only_components: [acme-api]
```

### @mentions and aliases

From Telegram or any chat-ops integration:

| Command | Effect |
|---|---|
| `@acme` or `@acme-api` | Route to group or project; runs `HIVEPILOT_DEFAULT_PIPELINE` |
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

### Analytics API (Phase 24a — SLA / duration / volume)

Read-only aggregate endpoints over the existing run store — no schema change, no writes. Feeds SLA dashboards, trend charts, and duration/latency reporting on top of `runs`/`steps`/`approvals`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/analytics/summary` | Totals + outcome rates, overall and grouped by `project`/`task`/raw `status` |
| `GET /v1/analytics/trends?bucket=day\|week` | Time-series run counts (+ outcome split), bucketed on `started_at` |
| `GET /v1/analytics/durations` | p50/p95/p99 + min/max/avg run duration (`finished_at - started_at`), overall and by `project`/`task` |
| `GET /v1/analytics/steps/failures` | `steps` grouped by (`step`, `status`), ranked with highest-failure-count combos first |
| `GET /v1/analytics/approvals/latency` | p50/p95 (+min/max/avg/count) of `approved_at - requested_at` |
| `GET /v1/analytics/providers` | `steps` grouped by `provider` and by `model` (counts + outcome split) — see Phase 24b.1 below |
| `GET /v1/analytics/cost` | Token + cost totals, overall and grouped by `provider`/`model` — see "Cost analytics" below |

Registered both unversioned (`GET /analytics/...`) and under `/v1`, matching every other route in this API.

**Auth & tenant scoping:** every endpoint requires `Depends(require_role("read"))` — the lowest role tier, so any valid token may call these. Results are filtered to the caller's tenant exactly like `GET /runs`/`GET /approvals`; **admin** tokens see all tenants, non-admin tokens never see another tenant's data.

**Common query params:** `days` (default 30, relative window) and optional `project`/`task` filters. All endpoints are read-only — they never mutate run state.

**CSV export:** append `?format=csv` to any endpoint for a `text/csv` response instead of JSON (e.g. `GET /v1/analytics/durations?format=csv`).

**PDF export (Phase 24 follow-up):** append `?format=pdf` to any analytics endpoint for an `application/pdf` response (a title + a table of the same rows/columns the CSV export uses), e.g. `GET /v1/analytics/durations?format=pdf`. Requires the **optional** `pdf` extra — `pip install hivepilot[pdf]` (pulls in `fpdf2`; not part of the core `api` extra or install). Without it installed, `?format=pdf` returns `501` with `"PDF export requires the 'pdf' extra: pip install hivepilot[pdf]"` — never a 500. Same auth/tenant scoping as JSON/CSV — the PDF path renders the identical tenant-scoped query result, just as a different content type.

**Canonical outcome mapping** (single source of truth: `hivepilot.services.analytics_service.canonical_outcome`, shared by the Textual dashboard):

| Raw `status` | Canonical outcome |
|---|---|
| `success`, `complete` | `succeeded` |
| `failed`, `denied`, `rate_limit`, `auth_expired`, `test_failure`, `security_blocker` | `failed` |
| `deferred` | `skipped` |
| anything else (`running`, `pending`, `new`, `planned`, `paused`, `review`, `approval`, `awaiting_approval`, ...) | `other` |

**Percentiles:** computed in Python (SQLite has no percentile aggregate) using the **nearest-rank method**: for `n` sorted values and percentile `p`, `index = ceil(p/100 * n) - 1` (clamped to `[0, n-1]`). Deterministic; always returns an observed value, never an interpolated one.

### Provider/model analytics (Phase 24b.1 — safe first step of cost/provider tracking)

Every step now persists **which provider and model executed it**, additively:

- `steps.provider` — the runner **kind** that ran the step (e.g. `claude`, `shell`, `codex`, `cursor`), or the resolved API provider (e.g. `openai`, `anthropic`) for a prompt-CLI runner configured in API mode. `NULL` when genuinely unknown (e.g. a non-native-engine placeholder step, or a multi-model debate step).
- `steps.model` — the model string resolved for the step. `NULL` when the step has no model at all (e.g. a `shell` runner).

Both columns are added via the same idempotent `ALTER TABLE ... ADD COLUMN` migration pattern `init_db()` already uses for `tenant` — safe to run against an existing database, and `state_service.record_step(...)` accepts them as optional keyword arguments (`provider=None, model=None`) so every pre-existing caller is unaffected.

`GET /v1/analytics/providers` exposes this as read-only aggregates, mirroring the auth/tenant-scoping/CSV pattern of every other analytics endpoint above:

```json
{
  "by_provider": [
    {"provider": "claude", "total": 42, "outcomes": {"succeeded": 40, "failed": 2, "skipped": 0, "other": 0}, "outcome_rates": {...}}
  ],
  "by_model": [
    {"model": "claude-sonnet-4-6", "total": 42, "outcomes": {...}, "outcome_rates": {...}}
  ]
}
```

Steps with no recorded provider/model (including every step recorded before this sprint) group under the literal key `"unknown"` — never dropped, never invented.

**Out of scope for this sprint:** token counts and cost. No runner-output-format change was made — this sprint only records what the orchestrator already knows about *which* runner/model it dispatched to, not usage/cost data returned by that runner. Token/cost analytics is the next sub-sprint (Phase 24b.2).

### Cost analytics (Phase 24b.2b — closes Phase 24)

`GET /v1/analytics/cost` turns the token/cost columns persisted by Phase 24b.2a's opt-in usage capture (`steps.input_tokens`, `steps.output_tokens`, `steps.cost_usd`) into read-only cost/token aggregates, mirroring the auth/tenant-scoping/CSV pattern of every other analytics endpoint above.

**Price map** (`hivepilot.services.pricing`): a small default table of USD-per-1M-token rates (`input`/`output`) for a handful of common models. **These defaults are indicative and dated (2026-07-15) — not a live-updated price feed.** Override or extend it via `HIVEPILOT_LLM_PRICE_MAP` (JSON object, e.g. `{"my-model": {"input": 3.0, "output": 15.0}}`), which is **merged over** the built-in defaults per-model (an override for one model doesn't drop the others). `pricing.estimate_cost(model, input_tokens, output_tokens)` is a pure function — it never touches the DB or network — returning `None` when the model isn't priced or a token count is missing.

**Cost precedence, per step, in this order:**

1. **Self-reported** `steps.cost_usd` (set only when `claude_capture_usage` was on and the CLI's JSON envelope included `total_cost_usd`) — authoritative, always preferred when present.
2. **Estimated** from the price map via `pricing.estimate_cost(...)`, when the step has token counts and its model is priced (by default or via `HIVEPILOT_LLM_PRICE_MAP`).
3. **Unpriced** — no self-reported cost and no price-map match (unknown model, or no tokens recorded at all). Contributes `0.0` to the cost total but is counted separately so the total is never silently presented as complete.

**Response shape:**

```json
{
  "overall": {"total_steps": 42, "input_tokens": 120000, "output_tokens": 45000, "cost_usd": 3.87, "unpriced_steps": 5},
  "by_provider": [
    {"provider": "claude", "total_steps": 40, "input_tokens": 118000, "output_tokens": 44500, "cost_usd": 3.85, "unpriced_steps": 3}
  ],
  "by_model": [
    {"model": "claude-sonnet-4-6", "total_steps": 40, "input_tokens": 118000, "output_tokens": 44500, "cost_usd": 3.85, "unpriced_steps": 3}
  ]
}
```

`unpriced_steps` is the **coverage number**: it's present at every scope (`overall`, and each `by_provider`/`by_model` row) so a dashboard can show "N of M steps had no cost signal" instead of quietly under-reporting spend. Steps with no recorded provider/model group under the literal key `"unknown"`, same as `GET /v1/analytics/providers`. CSV export (`?format=csv`) uses the same formula-injection guard as every other analytics endpoint.

Auth/tenant scoping/query params (`days`, `project`, `task`) are identical to `GET /v1/analytics/providers`.

**This closes Phase 24** (analytics API — SLA/duration/volume, provider/model breakdown, and cost analytics are all now shipped).

### Mirador web UI surface (Sprint 1) — plugin health & mem0 memory search

> The browser UI itself (app shell, token gate, install/enable/reverse-proxy
> notes) is documented separately in `docs/v4/WEBUI.md`. This section covers
> only the two API endpoints below.

Two small read-only endpoints for the Mirador web UI, siblings of the Analytics API above (same `require_role(...)`/dual-registration conventions), but neither is tenant-scoped:

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /v1/plugins/health` | `read` | Plugin health — same data as `PluginManager.check_all()` / the `plugins health` CLI command |
| `GET /v1/memories?query=...&limit=20` | **`admin`** | Semantic search proxy over mem0 (Mirador Mem0 view) |

Both registered unversioned (`GET /plugins/health`, `GET /memories`) and under `/v1`, matching every other route in this API.

**`GET /v1/plugins/health`** returns `{"plugins": [{"name": ..., "status": "ok"|"degraded"|"error", "detail": ...}, ...]}`. Health is process-global plugin state, not partitioned by tenant — every valid `read` token sees the same result, like `GET /v1/tasks`. `PluginManager.check_all()` never raises (a raising health check is caught and normalized to `HealthStatus("error", ...)`), so this endpoint cannot 500 on a broken check. `HealthStatus.detail` is either the plugin author's own hand-written status string (Phase 19 no-leak discipline, enforced by every shipped health check, e.g. `plugins/mem0.py`'s `health()` — only presence/mode booleans, never a secret/token value) or, when a health check raises unexpectedly, only the exception's **type name** (e.g. `"RuntimeError"`) — the exception message itself is logged server-side and never echoed into the response.

**`GET /v1/memories`** proxies a mem0 `search(query, limit=limit)` call, built the same way `plugins/mem0.py` builds its client (lazy import, `settings.mem0_*`). Graceful when mem0 is unconfigured — `mem0_enabled` off (the default), `mem0ai` not installed, or the client can't be built — returns `HTTP 200` with `{"configured": false, "memories": [], "detail": "..."}`, never a 500. A `client.search()` failure at call time degrades the same way. When configured: `{"configured": true, "memories": [{"memory": "...", "id": ..., "metadata": {...}, "score": ...}, ...]}`.

**Memories scoping rule (read this before wiring a `read` token to this endpoint):** mem0 memories carry `project`/`task`/`role` provenance metadata (`plugins/mem0.py`'s `_provenance_metadata`) but the mem0 store itself is **not** partitioned by HivePilot `tenant` — there is no `tenant` field on `ProjectConfig`/`projects.yaml` anywhere in this codebase, so "the caller's tenant's projects" cannot be derived to filter memories by. Rather than fabricate that mapping, `/v1/memories` is gated behind `require_role("admin")` instead of `"read"` — the same role that already sees unfiltered data on every analytics endpoint and on `GET /runs`/`GET /approvals`. **No `read`/`run`/`approve` token, regardless of tenant, can call this endpoint at all.** If a genuine tenant->project mapping is introduced later, this endpoint should be revisited to filter by it and reopened to `read` tokens.

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
