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
"cto":            runner=opencode, models=["opencode-go/kimi-k2.7-code"]  # Blaise, single-model
"developer":      runner=claude
"reviewer":       runner=codex, model="gpt-5.5"                  # Victor
"ciso":           runner=opencode, models=["opencode-go/glm-5.2"]  # Hugo, single-model
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
    acme:
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
acme-developer:
  role: developer                 # role drives runner+model (overrides the step runner)
  steps:
    - name: implementation
      runner: claude              # fallback if no role
      prompt_file: prompts/agents/developer.md
  git:
    commit: true
    push: true
    create_pr: true               # opens a PR via gh (when --auto-git + policy allows)
    draft: true                   # open it as a draft (gh pr create --draft)
    pr_title: "HivePilot: company pipeline implementation"
    branch_prefix: hivepilot

acme-release-gate:
  role: ciso                      # any can_block role's stage may carry the gate's git actions
  steps:
    - name: security-clearance
      runner: opencode
      prompt_file: prompts/agents/ciso.md
  git:
    promote_pr: true              # gh pr ready <branch> — marks the draft PR ready for review
    merge_pr: true                # optional: also merge once ready (method below)
    merge_method: squash          # merge | squash | rebase
    branch_prefix: hivepilot
```

- `draft` (on `create_pr`): open the PR via `gh pr create --draft`. Pair with a
  later gate stage's `promote_pr` so the PR only becomes visible for review once
  a `can_block` role's own verdict clears it.
- `promote_pr`: `gh pr ready <branch>` — promotes an existing draft PR to ready.
  **Gated on the stage's own agent report**: `promote_pr` runs *unless* that
  stage's parsed `status:` is an explicit blocking verdict — one of
  `BLOCK | BLOCKED | REJECT | REJECTED | REQUEST_CHANGES | CHANGES_REQUESTED |
  NEEDS_HUMAN | FAIL | FAILED | DENY | DENIED` — in which case it is skipped
  (logged as `git.promote_skipped_blocked`). The agent status vocabulary is
  heterogeneous: `PASS`, `APPROVE`, `APPROVED`, `CLEARED`, `ADVISORY`, `OK` all
  mean "proceed", so a blocking-verdict **blacklist** (not a PASS-only
  whitelist) is used — the release gate approving with `status: APPROVE` still
  promotes. Absent/unstructured stage output is likewise non-blocking (legacy
  behaviour for tasks that aren't `can_block` roles).
- `merge_pr` (previously undocumented): `gh pr merge <branch> --<merge_method>`
  — Jules' autonomous final approval, since GitHub forbids approving your own
  PR. `merge_method` is `merge` (default) | `squash` | `rebase`. `merge_pr` is
  gated by the same explicit-blocking-verdict check as `promote_pr`, and (when
  both flags are set) `promote_pr` always runs before `merge_pr`.

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

### Usage capture (tokens/cost/actual-model) — opt-in

`HIVEPILOT_CLAUDE_CAPTURE_USAGE` (default `false`) enables per-step token/cost/
actual-model capture from the claude runner (Phase 24b.2a). Default **off** is
byte-identical to today's behaviour: `capture()` invokes `claude` without
`--output-format json` and returns raw stdout, exactly as before this flag
existed.

When **on**, `capture()` adds `--output-format json`, parses the CLI's JSON
envelope, and:

- still returns only the agent's `result` text as the step output (unchanged
  from the caller's point of view — turning this flag on never changes what
  an agent's output looks like downstream)
- additionally records `input_tokens` / `output_tokens` (from the envelope's
  `usage` object), `total_cost_usd`, and the `model` actually used — persisted
  on `steps.input_tokens` / `steps.output_tokens` / `steps.cost_usd`, and the
  actual `model` overrides the config-resolved model recorded on `steps.model`
  (closing the gap where a profile- or default-model claude step otherwise
  persisted `NULL` for `model` — see the provider/model persistence above)

**Graceful degradation guarantee:** this flag can only ever make a step
behave like flag-off — it can never make a working step fail, and it can
never corrupt step output. If the JSON is malformed, missing the `result`
field, or the CLI errors on the `--output-format json` flag itself (e.g. an
older claude CLI build that doesn't support it), the runner falls back to
raw-text output with `NULL` usage and logs a one-line warning (step/project
name + failure kind only — never output content, tokens, or secrets). A step
that would have succeeded with the flag off always still succeeds with the
flag on.

**Cost is CLI-self-reported only** at capture time — a runner/provider that
doesn't self-report `total_cost_usd` persists `NULL` for `cost_usd`. The
price-map fallback (Phase 24b.2b) doesn't backfill `steps.cost_usd` itself;
it's applied read-only, at query time, by `GET /v1/analytics/cost` (see
"Price map & cost analytics" below).

### Price map & cost analytics (Phase 24b.2b — closes Phase 24)

`hivepilot.services.pricing` supplies a small default USD-per-1M-token price
table (`input`/`output` rate per model), used as a **fallback** estimate by
`GET /v1/analytics/cost` (`hivepilot.services.analytics_service.cost_summary`)
whenever a step has no self-reported `cost_usd`. **The defaults are
indicative and dated (2026-07-15), not a maintained live price feed** —
override or extend them via `HIVEPILOT_LLM_PRICE_MAP`, a JSON object merged
**over** the defaults per-model:

```bash
HIVEPILOT_LLM_PRICE_MAP='{"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}, "my-custom-model": {"input": 1.0, "output": 2.0}}'
```

**Per-step cost precedence** (see `analytics_service._step_cost`):

1. self-reported `steps.cost_usd` (authoritative, when `claude_capture_usage` captured it)
2. estimated from the price map (`pricing.estimate_cost`), when tokens are recorded and the model is priced
3. unpriced — contributes `0.0` to the total, counted in the response's `unpriced_steps` coverage number so a dashboard never presents an incomplete total as if it were exhaustive

See `docs/v4/RUNBOOK.md` "Cost analytics" for the `GET /v1/analytics/cost` endpoint shape.

## Key environment variables / settings

| Setting | Env | Default |
|---|---|---|
| obsidian_vault | `HIVEPILOT_OBSIDIAN_VAULT` | `…/obsidian-vault/Acme` |
| container_runtime | `HIVEPILOT_CONTAINER_RUNTIME` | `docker` (or `podman`; per-runner override via `options.runtime`) |
| claude_permission_mode | `HIVEPILOT_CLAUDE_PERMISSION_MODE` | — (global fallback; developer role already sets `bypassPermissions`) |
| claude_capture_usage | `HIVEPILOT_CLAUDE_CAPTURE_USAGE` | `false` — opt-in per-step token/cost/actual-model capture; see "Usage capture" above |
| llm_price_map | `HIVEPILOT_LLM_PRICE_MAP` | — (JSON object, merged over `pricing.DEFAULT_PRICE_MAP`); see "Price map & cost analytics" above |
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
