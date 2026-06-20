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
"chief_of_staff": runner=cursor
"cto":            runner=opencode, model="opencode-go/kimi-k2.7-code"
"developer":      runner=claude
"reviewer":       runner=codex
"ciso":           runner=opencode, model="opencode-go/glm-5.2"
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

## Key environment variables / settings

| Setting | Env | Default |
|---|---|---|
| obsidian_vault | `HIVEPILOT_OBSIDIAN_VAULT` | `…/obsidian-vault/Noxys` |
| state_db | `HIVEPILOT_STATE_DB` | `state.db` |
| telegram_bot_token | `HIVEPILOT_TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` | — |
| telegram_allowed_chat_ids | `HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS` | `[]` (open) |
| telegram_stream_live | `HIVEPILOT_TELEGRAM_STREAM_LIVE` | `true` (live-stream each agent turn to Telegram; silent no-op if Telegram/notification chat id unset) |
| gh_command / git_command | — | `gh` / `git` |

(Settings are `pydantic-settings`; any field is overridable via `HIVEPILOT_<NAME>`.)
