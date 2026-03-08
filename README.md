# 🐝 HivePilot v2

HivePilot is an AI command center for multi-repo workflows. It dispatches Claude Code, LangChain, LangGraph, CrewAI, shell runners, Codex/Gemini/OpenCode/Ollama CLIs, and Git/GitHub automation from a single YAML-driven orchestrator. This release adds an interactive TUI, concurrency, structured logging + run history, state persistence, and optional API fallbacks for every runner.

---

## ✨ Highlights

- **Interactive mode** – `hivepilot interactive` (Questionary) lets you choose projects/tasks/pipelines on the fly.
- **Parallel execution** – ThreadPool-backed scheduling spreads a task/pipeline across many repositories (`--concurrency` or `.env`).
- **YAML-first runners** – Define Claude/shell/LangChain/internal/Codex/Gemini/OpenCode/Ollama/OpenRouter runners once and reference them everywhere.
- **CLI ↔ API switch** – Every CLI runner can flip to API mode (OpenAI, Anthropic, Google Gemini, Mistral, Perplexity, OpenRouter) per YAML, CLI being the default fallback.
- **Pipelines + multi-step API/CLI workflows** – Chain tasks with mixed engines (API pre-check → CLI codemod → shell validation).
- **Structured logging & state store** – `runs/<timestamp>/summary.json` + JSON logs + SQLite `state.db` capture every run for later inspection, TUI dashboards, and scheduling.
- **Git/GitHub automation** – Built-in services handle branch/commit/push, `gh repo/issue/release`, and YAML tasks (`gh-*`) for declarative automation.
- **Rich extras** – LangGraph, LangChain, CrewAI, Textual dashboard, scheduler, and profile-driven Claude model selection (Sonnet/Opus/Haiku).
- **Discovery + remote API** – `hivepilot discover` scans local/GitHub repos, and `hivepilot api serve` exposes FastAPI endpoints for remote triggers/ChatOps.
- **Policies & notifications** – `policies.yaml` defines per-project rules (auto-git/approvals) and Slack/Discord/Telegram webhooks notify on start/completion/failure.
- **Secrets & knowledge-aware prompts** – Steps reference `secrets:` blocks (env/SOPS/etc.) and `knowledge_files:` to inject repo context via LangChain/FAISS embeddings.
- **RBAC + tokens** – CLI/API commands enforce `read`, `run`, `approve`, and `admin` roles. Manage tokens via `hivepilot tokens …` or add them to `api_tokens.yaml`; supply tokens via `--token` or `HIVEPILOT_API_TOKEN`.
- **ChatOps** – Slack slash commands (and optional Telegram bot) can list/approve runs. `POST /chatops/slack` accepts Slack payloads; use `HIVEPILOT_CHATOPS_TOKEN` to authorize ChatOps flows.

---

## 📂 Architecture Snapshot

```
hivepilot/
├── hivepilot/
│   ├── cli.py              # Typer CLI + interactive mode + gh subcommands
│   ├── config.py           # Pydantic Settings (.env)
│   ├── orchestrator.py     # Scheduler, concurrency, pipelines
│   ├── registry.py         # Maps runner names to implementations
│   ├── models.py           # Pydantic schemas for projects/tasks/pipelines
│   ├── pipelines.py        # Pipeline helpers
│   ├── runners/            # Claude, shell, LangChain, Codex/Gemini/OpenCode/Ollama/OpenRouter
│   ├── services/           # git_service, github_service, project_service, pipeline_service
│   └── utils/              # io (runs/summary), logging (structlog), shell helpers
├── prompts/
├── projects.yaml
├── tasks.yaml
├── pipelines.yaml
├── model_profiles.yaml     # Claude profile map (coding/architecture/automation)
├── .env.example
├── requirements.txt
└── README.md
```

Everything is configured via YAML (`projects`, `tasks`, `pipelines`, `model_profiles`). `.env` only tweaks global paths/commands.

---

## ⚙️ YAML Reference

### projects.yaml

```yaml
projects:
  example-api:
    path: ~/dev/example-api
    description: Example backend service
    claude_md: CLAUDE.md
    default_branch: main
    owner_repo: your-user/example-api
    env:
      PYTHONUNBUFFERED: "1"
```

### tasks.yaml

```yaml
runners:
  claude-docs:
    kind: claude
    command: claude
    options:
      profile: automation        # maps to model_profiles.yaml
  validation-suite:
    kind: shell
    command: |
      if [ -f package.json ]; then npm test || true; fi
      if [ -f pyproject.toml ]; then pytest || true; fi
  codex-default:
    kind: codex
    command: codex
    options:
      mode: cli                  # default, switch to api when needed
      api_provider: openai
      api_model: gpt-4o

  container-validation:
    kind: container
    command: |
      pip install -r requirements.txt && pytest
    options:
      image: python:3.11
      volumes:
        - ${PWD}:/workspace
        - /tmp/cache:/workspace/.cache

tasks:
  docs:
    description: Rewrite documentation
    steps:
      - name: rewrite docs
        runner: claude-docs
        prompt_file: prompts/docs_rewrite.md
        metadata:
          claude_profile: automation
          knowledge_files: ["README.md", "docs/architecture.md"]
        secrets:
          OPENAI_API_KEY:
            source: env
            key: OPENAI_API_KEY
    artifacts:
      capture: ["diff"]
      exporters:
        - target: local
        - target: s3
          bucket: hivepilot-artifacts
          prefix: docs-runs
    git:
      commit: true
      push: true
      create_pr: true

  codex-audit:
    description: Architecture scan via Codex (CLI/API)
    steps:
      - name: codex review
        runner: codex-default
        prompt_file: prompts/architecture_review.md

  refactor:
    description: Refactor the codebase with a light validation pass.
    steps:
      - name: refactor
        runner: claude
        runner_ref: claude-refactor
        prompt_file: refactor.md
        timeout_seconds: 5400
      - name: validation
        runner: container
        runner_ref: container-validation
        allow_failure: true
        timeout_seconds: 1800

  gh-repo-init-task:
    description: Provision the GitHub repo through internal runners
    steps:
      - name: repo init
        runner: shell
        command: hivepilot gh repo-init {project_name} --set-remote --push
```

#### Command templating

Shell/CLI commands accept `{variables}`: `project_name`, `project_path`, `project_default_branch`, `project_owner_repo`, `task_name`, `step_name`, `extra_prompt`. Escape braces via `{{` / `}}`.

### Model profiles (`model_profiles.yaml`)

```yaml
claude_profiles:
  coding:
    model: sonnet      # best for coding
  architecture:
    model: opus        # deep reasoning / architecture
  automation:
    model: haiku       # fast automations
```

Reference profiles via `metadata.claude_profile` or runner `options.profile`. Add your own (e.g., `review`, `summary`).

### CLI ↔ API switch

Set `options.mode: api` (or `metadata.mode`) to call APIs instead of CLIs. Supported `api_provider` values:

- `openai`, `anthropic`, `google`, `mistral`, `perplexity`, `openrouter`.

Required env vars:

| Provider      | Env var             |
|---------------|---------------------|
| OpenAI        | `OPENAI_API_KEY`    |
| Anthropic     | `ANTHROPIC_API_KEY` |
| Google Gemini | `GOOGLE_API_KEY`    |
| Mistral AI    | `MISTRAL_API_KEY`   |
| Perplexity    | `PERPLEXITY_API_KEY`|
| OpenRouter    | `OPENROUTER_API_KEY`|

CLI remains the default fallback; switching back is as simple as removing `mode: api`.

### pipelines.yaml

```yaml
pipelines:
  pentest-fix-review:
    description: Pentest → refactor → docs
    stages:
      - name: pentest
        task: pentest
      - name: refactor follow-up
        task: refactor
      - name: docs summary
        task: docs

  gh-repo-init:
    description: Ensure GitHub repo exists & push default branch
    stages:
      - name: initialize repo
        task: gh-repo-init-task
```

### policies.yaml

```yaml
policies:
  default:
    allow_auto_git: true
    require_approval: false
    allow_containers: true
  projects:
    example-api:
      allow_auto_git: false
      require_approval: true
      allow_containers: false
```

Policies are evaluated before every run. If `allow_auto_git` is `false`, `--auto-git` is blocked for that project. Extend via plugins to enforce approvals or multi-factor flows.

#### Approval workflow

- Runs on projects with `require_approval: true` are queued until approved.
- Review pending runs via `hivepilot approvals list` or the `GET /approvals` API.
- Approve/deny via CLI (`hivepilot approvals approve <run_id>`), API (`POST /approvals/{run_id}`), or respond via Slack/Discord/Telegram if you wire those webhooks to the API endpoint.
- Notifications include the run ID so approvers know what to act on. Once approved, the orchestrator resumes the run with the same run ID and updates `state.db`.

### schedules.yaml

```yaml
schedules:
  docs-weekly:
    task: docs
    projects: ["example-api"]
    interval_minutes: 10080
    enabled: true
```

Use `hivepilot schedule list` to inspect schedules and `hivepilot schedule run` to execute those whose interval has elapsed. Schedule timestamps are tracked in `state.db`.

### Notifications (Slack/Discord/Telegram)

Set any combination of:

- `SLACK_WEBHOOK_URL`
- `DISCORD_WEBHOOK_URL`
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`

When present, HivePilot sends start/completion/failure notifications automatically. For richer flows (approvals, custom alerts), add plugins under `plugins/` or point `HIVEPILOT_PLUGINS_ENTRY` to your module.

### api_tokens.yaml (RBAC)

```yaml
tokens:
  - token: 0123abcd...
    role: admin
    note: "local admin"
  - token: deadbeef...
    role: run
    note: "CI pipeline"
```

Manage tokens with `hivepilot tokens add/list/remove` (admin role required). Roles map to permissions:

| Role     | Permissions                                |
|----------|--------------------------------------------|
| `read`   | list projects/tasks/schedules/approvals    |
| `run`    | trigger tasks/pipelines, schedule runs     |
| `approve`| approve/deny queued runs                   |
| `admin`  | manage tokens, policies, API server        |

CLI commands require `--token <value>` (or set `HIVEPILOT_API_TOKEN`). API requests must include `Authorization: Bearer <token>`. Tokens are stored in `api_tokens.yaml` and synced to `state.db` for quick lookup.

### ChatOps (Slack/Discord/Telegram)

- Set `HIVEPILOT_CHATOPS_TOKEN` to a token with `run`/`approve` permissions.
- **Slack**: configure slash commands to hit `POST /chatops/slack`.
  - `/hivepilot-run <project> <task>`
  - `/hivepilot-approvals`
  - `/hivepilot-approve <run_id>` / `/hivepilot-deny <run_id>`
- **Discord**: send messages such as `!hp run <project> <task>` or `!hp approvals` to the endpoint bound to `POST /chatops/discord`.
- **Telegram**: point your bot webhook to `POST /chatops/telegram` and use `/hp_run`, `/hp_approvals`, `/hp_approve`, `/hp_deny`.

---

## 🧑‍💻 Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

Optional extras:

```bash
pip install -e .[langgraph]
pip install -e .[crewai]
pip install -e .[full]    # langgraph + crewai + textual + langchain extras
```

Docker:

```bash
docker compose build
docker compose run --rm hivepilot hivepilot doctor
```

---

## 🕹 CLI Cheat Sheet

```bash
hivepilot lint
hivepilot doctor
hivepilot list-projects
hivepilot list-tasks
hivepilot list-pipelines
hivepilot run example-api docs
hivepilot run example-api docs --project example-site --concurrency 2
hivepilot run example-api pentest --all --auto-git
hivepilot run example-api gh-issue-from-extra --extra-prompt "Docs refresh"
hivepilot run example-api docs --extra-prompt "Focus on auth"   # uses knowledge-aware prompts
hivepilot run example-api codex-audit
hivepilot run example-api gemini-brief
hivepilot run example-api opencode-fix
hivepilot run example-api ollama-scan
hivepilot run-pipeline example-api pentest-fix-review
hivepilot interactive
hivepilot dashboard                  # Textual run history
hivepilot approvals list             # show pending approvals
hivepilot approvals approve 42 --approver alice
hivepilot tokens add --role run --note "CI worker"
hivepilot tokens list
hivepilot tokens remove 0123abcd...

# GitHub helpers
hivepilot gh repo-init example-api --push
hivepilot gh issue example-api "Docs refresh" --body "Regénérer README"
hivepilot gh release example-api v0.2.0 --title "Docs refresh"

# API / scheduler / plugins
hivepilot api serve --host 0.0.0.0 --port 8045
curl -X POST http://localhost:8045/run \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer <your-token>" \
     -d '{"task":"docs","projects":["example-api"],"extra_prompt":"Focus on auth"}'
# (Upcoming scheduler commands hook into the same API/state store.)

# Discovery helpers
hivepilot discover --root ~/dev --max-depth 2
hivepilot discover --github-org your-org

# Scheduler helpers
hivepilot schedule list
hivepilot schedule run
# All commands accept --token / HIVEPILOT_API_TOKEN to enforce RBAC
# Metrics endpoint
curl http://localhost:8045/metrics
```

---

## 📊 Logging, State & Dashboard

- Each run writes `runs/<timestamp>/summary.json`.
- Structlog JSON logs land in `runs/logs/hivepilot.log`.
- `.env` `HIVEPILOT_OUTPUT_FORMAT` can switch summary format (json/plain).
- SQLite `state.db` records run metadata for dashboards, exports, schedulers.
- `hivepilot dashboard` (requires `HIVEPILOT_ENABLE_TEXTUAL_UI=true`) opens a Textual UI to browse history, details, and active runs.

---

## 🧠 Engines, Runners & Plugins

- **Native** – Claude/shell workflows via the runner registry.
- **LangGraph** – reference `graph: module:function` to compile/invoke graphs.
- **CrewAI** – tasks/pipelines can point to a `build_crew` builder in `workflows/`.
- **LangChain** – runner loads an `LLMChain`.
- **CLI/API hybrids** – Codex, Gemini, OpenCode, Ollama, OpenRouter runners flip between CLI (fast, offline) and API (hosted) per YAML.
- **Multi-step workflows** – mix API and CLI steps in pipelines (e.g., API analysis, CLI codemod, shell validation).
- **Plugins hooks** – drop Python files in `plugins/` (or set `HIVEPILOT_PLUGINS_ENTRY=module:function`) to register hooks like `before_step` / `after_step`, enabling Slack notifications, approvals, vulnerability scanners, etc.
  - Example `plugins/sample.py` logs every step; use it as a starting point for Slack/email approvals or external scanners.
- **Artifacts** – after each run, `runs/<timestamp>/artifacts` contains `results.json`, Git patches (if enabled), etc. Configure exporters (local/S3) via `task.artifacts` to ship results automatically.
- **Container runner** – run steps inside Docker images by setting `kind: container` with image/command options; policies can block/allow container use per project.

Add new runners by dropping a Python class in `hivepilot/runners/` and registering it in the `RUNNER_MAP`.

---

## 🐙 Git + GitHub Services

- `git_service.py` handles checkout, add/commit, push, auto-git enforcement.
- `github_service.py` wraps `gh repo/issue/release` (with retries, templated URLs).
- Use YAML `gh-*` tasks or CLI `hivepilot gh repo-init|issue|release` to manage repos, issues, releases.

---

## ✅ Quick Smoke Tests

1. `hivepilot tokens add --role admin --note "local admin"` → copy the token and `export HIVEPILOT_API_TOKEN=<token>`
2. `hivepilot lint`
3. `hivepilot doctor`
4. `hivepilot run example-api docs --dry-run`
5. `hivepilot run example-api gh-repo-init-task`
6. `hivepilot run example-api gh-issue-from-extra --extra-prompt "Docs refresh"`
7. `hivepilot run-pipeline example-api pentest-fix-review --concurrency 2`
8. `hivepilot dashboard` (after `export HIVEPILOT_ENABLE_TEXTUAL_UI=true`)
9. `hivepilot run example-api codex-audit --extra-prompt "Security scan"`
10. `hivepilot run example-api docs-langgraph --auto-git`
11. `hivepilot api serve` + `curl http://localhost:8045/run ...` (with `Authorization: Bearer <token>`)
12. `hivepilot schedule list` / `hivepilot schedule run`
13. `export SLACK_WEBHOOK_URL=...` (or Discord/Telegram) and re-run to confirm notifications
14. `curl http://localhost:8045/metrics`
15. `hivepilot run example-api docs --extra-prompt "Focus on auth"` to see knowledge-aware prompts/secrets

Each run should create a folder under `runs/<timestamp>` containing summaries, logs, and (optionally) artifacts or state references.
