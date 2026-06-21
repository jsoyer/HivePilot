# HivePilot V4 — Configuration

All config lives at the repo root (resolved via `settings.resolve_config_path`).

| File | Defines |
|---|---|
| `projects.yaml` | Target repos (path, default_branch, owner_repo, env) |
| `tasks.yaml` | `runners:` (CLI definitions) + `tasks:` (steps, prompt_file, **role**, git actions) |
| `pipelines.yaml` | Pipelines = ordered list of stages (name → task) |
| `roles.py` | The role registry: role → runner kind + model(s) (global defaults) |
| `policies.yaml` | Per-project policy + **role/runner overrides** |
| `config/model_profiles.yaml` | Documentary mirror of the role→runner/model map |
| `prompts/agents/<role>.md` | Each role's system prompt |

## roles.py (the company defaults)

```python
"ceo":            runner=opencode, models=["opencode-go/qwen3.7-max","opencode-go/kimi-k2.6"]  # dual → debate
"chief_of_staff": runner=cursor                                  # Jules (CSO)
"cto":            runner=opencode, models=["opencode-go/kimi-k2.7-code","claude:claude-sonnet-4-6"]  # Blaise, dual->debate
"developer":      runner=claude
"reviewer":       runner=codex, model="gpt-5.5"                  # Victor
"ciso":           runner=opencode, models=["opencode-go/glm-5.2","claude:claude-haiku-4-5"]  # Hugo, dual->debate
"qa":             runner=cursor       # dedicated QA runner (distinct from docs)
"documentation":  runner=gemini
```
`resolve_runner(role, policy)` = these defaults + per-project overrides.

## policies.yaml (per-project)

```yaml
policies:
  default:
    allow_auto_git: true
    require_approval: false
    allow_containers: true
  projects:
    noxys:
      allow_auto_git: true        # developer may push + open PR
      require_approval: true      # every run waits for human /approve
      allow_containers: false
      # Optional per-project model/runner overrides:
      # allowed_runners: [opencode, claude]      # whitelist; resolved runner must be in it
      # role_overrides:
      #   cto: { model: opencode-go/glm-5.2 }    # keep runner, change model
      #   qa:  { runner: claude }                # change runner
```

- `require_approval: true` → runs queue (`approvals`) until a human approves; `--simulate` bypasses it.
- `allow_auto_git` is enforced: requesting `--auto-git` against a project that forbids it raises.
- `role_overrides` / `allowed_runners` are applied by `resolve_runner`.

## tasks.yaml (a task)

```yaml
company-developer:
  role: developer                 # role drives runner+model (overrides the step runner)
  steps:
    - name: implementation
      runner: claude              # fallback if no role
      prompt_file: prompts/agents/developer.md
  git:
    commit: true
    push: true
    create_pr: true               # opens a PR via gh (when --auto-git + policy allows)
    pr_title: "HivePilot: company pipeline implementation"
    branch_prefix: hivepilot
```

## Runner non-interactive invocation

Each CLI runner is invoked headlessly (so real runs don't hang); overridable via
the runner's `options`:

| Runner | Invocation |
|---|---|
| claude / cursor-agent | `--print` flag |
| gemini | `-p "<prompt>"` |
| codex | `exec` subcommand |
| opencode | `run` subcommand, model as `provider/model` (e.g. `opencode-go/kimi-k2.7-code`) |
| vibe (Mistral) | `--prompt "<prompt>"` + `--auto-approve`; no `--model` (model via its own config / `MISTRAL_API_KEY`) |

Override example: `options: { subcommand: exec, model_flag: "-m", prompt_flag: "-p" }`.

### Headless permission mode (autonomous dev)

`claude --print` cannot show an interactive permission prompt, so an agent that
needs to edit files / run commands **hangs to timeout writing nothing** unless a
permission mode is passed. The developer role (Gustave) ships with
`permission_mode="bypassPermissions"` so it writes code and runs the test suite
autonomously — gated by the human plan checkpoint that precedes the Implementation
stage, and scoped to the component repo.

Precedence (first wins): step `metadata.permission_mode` → runner
`options.permission_mode` → role `permission_mode` (roles.py) → global
`HIVEPILOT_CLAUDE_PERMISSION_MODE`. Values: `acceptEdits` (edits only, shell still
gated), `bypassPermissions` (full autonomy), `plan`, `default`. Unset = no flag
(safe for read-only planning agents).

## Key environment variables / settings

| Setting | Env | Default |
|---|---|---|
| obsidian_vault | `HIVEPILOT_OBSIDIAN_VAULT` | `…/obsidian-vault/Noxys` |
| container_runtime | `HIVEPILOT_CONTAINER_RUNTIME` | `docker` (or `podman`; per-runner override via `options.runtime`) |
| claude_permission_mode | `HIVEPILOT_CLAUDE_PERMISSION_MODE` | — (global fallback; developer role already sets `bypassPermissions`) |
| state_db | `HIVEPILOT_STATE_DB` | `state.db` |
| telegram_bot_token | `HIVEPILOT_TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` | — |
| telegram_allowed_chat_ids | `HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS` | `[]` (open) |
| telegram_stream_live | `HIVEPILOT_TELEGRAM_STREAM_LIVE` | `true` (live-stream each agent turn to Telegram; silent no-op if Telegram/notification chat id unset) |
| telegram_stream_topics | `HIVEPILOT_TELEGRAM_STREAM_TOPICS` | `false` — When `true` AND `telegram_stream_chat_id` is set, each agent's live-stream turns are routed to their own forum topic in the supergroup. The bot must be admin of the forum supergroup with the `manage_topics` permission. Topic thread IDs are persisted to `.hivepilot/stream_topics.json`. |
| gh_command / git_command | — | `gh` / `git` |

(Settings are `pydantic-settings`; any field is overridable via `HIVEPILOT_<NAME>`.)

## Token-saving caching (L1–L3)

| Setting | Env | Default | Description |
|---|---|---|---|
| `anthropic_prompt_cache` | `HIVEPILOT_ANTHROPIC_PROMPT_CACHE` | `True` | When True, sends prompts as a cacheable system block with `cache_control: ephemeral` to Anthropic. Disable to use plain messages format. |
| `prior_context_mode` | `HIVEPILOT_PRIOR_CONTEXT_MODE` | `cap` | How to build the inter-agent prior_context. `cap`: truncate to `max_prior_context_chars` keeping the tail. `synthesis`: keep only the Plan Synthesis chunk + last chunk. `full`: original join-all behaviour. |
| `max_prior_context_chars` | `HIVEPILOT_MAX_PRIOR_CONTEXT_CHARS` | `8000` | Max characters for `prior_context_mode=cap`. Content beyond this limit is trimmed from the head. |
| `stage_cache_enabled` | `HIVEPILOT_STAGE_CACHE_ENABLED` | `False` | Opt-in SQLite stage memoization. When True, skips the runner on a cache hit and stores results on miss. Disabled when `simulate=True` or `auto_git=True`. |
| `cache_backend` | `HIVEPILOT_CACHE_BACKEND` | `sqlite` | Cache storage backend. `sqlite` reuses `state.db` (zero infra). `redis` requires `redis_url`. |
| `redis_url` | `HIVEPILOT_REDIS_URL` | — | Redis connection URL (e.g. `redis://localhost:6379`). Required when `cache_backend=redis`. |

**Default is SQLite (zero infra, reuses state.db).** Redis is opt-in for the distributed-workers setup (`cache_backend=redis` + `redis_url=redis://...`).
