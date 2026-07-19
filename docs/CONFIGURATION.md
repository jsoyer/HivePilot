# Configuration Reference

HivePilot is configured through a set of YAML files, each validated by a pydantic model at
load time, plus a `HIVEPILOT_`-prefixed environment layer for runtime settings.

Config files live at the workspace root by default (the current working directory in
development), but each file is resolved through a search chain, checked in this order:

1. `$XDG_CONFIG_HOME/hivepilot/<file>`
2. `config_repo/<file>` (an optional external config repo)
3. `base_dir/<file>` (the workspace root — cwd in dev)

The first file found wins. The `config_repo` set can be synced with GitOps workflows via
`hivepilot config sync` and `hivepilot config push` — see [CLI-REFERENCE.md](CLI-REFERENCE.md).

Every section below documents one config file: its pydantic model, the real field list, and
a minimal working YAML example.

## projects.yaml — `ProjectConfig`

Defines the repos HivePilot operates on. Top-level key is a project name; value is a
`ProjectConfig`.

Fields:

- `path` (required) — filesystem path to the project checkout
- `description`
- `claude_md` — path to a project-specific CLAUDE.md-style prompt file
- `default_branch` (default `"main"`)
- `owner_repo` — `owner/repo` slug, used for PR/git-host operations
- `env: dict[str, str]` — environment variables injected into runs for this project
- `secrets: dict[str, dict]` — maps `NAME` to a secret spec (`{source, ...}`); referenced
  in `env` values as `${secret:NAME}` and resolved at run time (see
  [SECURITY.md](SECURITY.md))

```yaml
projects:
  acme-api:
    path: /home/jerome/code/acme-api
    description: Acme's core API service
    default_branch: main
    owner_repo: acme-org/acme-api
    secrets:
      GITHUB_TOKEN:
        source: env
    env:
      GITHUB_TOKEN: "${secret:GITHUB_TOKEN}"
```

## tasks.yaml — `runners:` + `tasks:`

Defines reusable runner definitions and the tasks (sequences of steps) that use them.

### `RunnerDefinition`

- `name`
- `kind` (`RunnerKind` — e.g. `claude`, `codex`, `vibe`, `openrouter`, or a plugin-provided kind)
- `command`
- `model`
- `effort`
- `agent`
- `append_prompt`
- `timeout_seconds`
- `host`
- `env`
- `options`

### `TaskConfig`

- `description`
- `role`
- `engine` — `"native"` | `"langgraph"` | `"crewai"` (default `"native"`)
- `graph` — engine-specific graph definition (langgraph)
- `crew` — engine-specific crew definition (crewai)
- `steps` — list of `TaskStep`
- `git` — `GitActions`
- `options`
- `artifacts`

### `TaskStep`

- `name`
- `runner`
- `runner_ref`
- `prompt_file`
- `command`
- `allow_failure` (default `False`)
- `append_prompt`
- `timeout_seconds`
- `require_approval` (default `False`)
- `metadata`
- `knowledge_files`
- `secrets`
- `skills: list | None`
- `effort`

### `GitActions`

- `commit` (default `False`)
- `push` (default `False`)
- `create_pr` (default `False`)
- `draft` (default `False`)
- `merge_pr` (default `False`)
- `promote_pr` (default `False`)
- `merge_method` (default `"merge"`)
- `commit_message`
- `pr_title`
- `pr_body_file`
- `branch_prefix` (default `"hivepilot"`)

```yaml
runners:
  claude-dev:
    kind: claude
    model: claude-sonnet-4-6
    effort: medium

tasks:
  implement-feature:
    description: Implement a feature end-to-end
    role: developer
    engine: native
    steps:
      - name: implement
        runner: claude-dev
        prompt_file: prompts/developer.md
        timeout_seconds: 1800
    git:
      commit: true
      push: true
      create_pr: true
      branch_prefix: hivepilot
```

## roles.yaml — a LIST under `roles:`

Defines the roster of roles available to pipelines and tasks. `roles:` is a **list**, not a
dict.

### `Role`

- `name`
- `title`
- `prompt_file`
- `model_profile`
- `inputs: list`
- `outputs: list`
- `optional_inputs`
- `can_block: bool`
- `order: int`
- `runner`
- `model`
- `models: list | None`
- `display_name`
- `host`
- `permission_mode`
- `command_task`
- `effort`

If `roles.yaml` is missing or fails validation, HivePilot falls back to a single generic
`developer → claude` role. The full multi-role "company" roster (ceo, cto, ciso, developer,
documentation, ...) is config-owned — see `examples/roles.yaml` in the repo for a complete
starting point.

```yaml
roles:
  - name: ceo
    title: Chief Executive Officer
    prompt_file: prompts/ceo.md
    runner: claude
    model: claude-opus-4-1
    effort: high
    order: 0
    can_block: true
    inputs: []
    outputs: [strategy_brief]
```

### Role → runner/model/effort resolution

Each role resolves its runner, model, and effort at execution time. The precedence chain,
highest priority first:

```
policy.role_overrides > stage > role > runner-default
```

A `policies.yaml` `role_overrides` entry beats a pipeline stage's explicit setting, which
beats the role's own `runner`/`model`/`effort`, which beats the runner definition's default.
See [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md) for the full resolution walkthrough.

## pipelines.yaml — `PipelineConfig`

Defines multi-stage pipelines that chain tasks/roles together.

Fields:

- `description`
- `mode` — `"cli"` | `"api"` (default `"cli"`)
- `model`
- `effort`
- `stages: list[PipelineStage]`
- `debate: DebateConfig | None`
- `lessons: LessonsConfig | None`

### `PipelineStage`

- `name`
- `task`
- `mode`
- `model`
- `effort`
- `pause_before` (default `False`) — pauses for human plan-approval before the stage runs
- `commits_vault` (default `False`) — commits an Obsidian changelog entry after the stage
- `only_components: list | None`
- `only_tags: list | None`
- `continue_on_failure` (default `False`)
- `skills: list | None`
- `debate: DebateConfig | None`

`mode` resolves as `stage.mode or pipeline.mode or "cli"`. `model` and `effort` resolve the
same way, stage value first, then pipeline value, with no runner-level fallback baked into
the pipeline layer itself (role/runner resolution then applies on top — see roles.yaml
above). See [RUNNERS.md](RUNNERS.md) for what `cli` vs `api` mode implies at execution time.

```yaml
pipelines:
  ship-feature:
    description: Plan, implement, and review a feature
    mode: cli
    stages:
      - name: plan
        task: plan-feature
        pause_before: true
      - name: implement
        task: implement-feature
        model: claude-sonnet-4-6
      - name: review
        task: review-feature
        continue_on_failure: false
```

### Per-pipeline `debate:` and `lessons:` blocks

These blocks are **live and wired** — not aspirational. They are opt-in and default-off:
omitting them changes nothing about pipeline behavior.

#### `DebateConfig`

- `enable_judge`
- `enable_arbiter`
- `runner`
- `model`
- `confidence_threshold` — validated to be in `(0, 1]` at config load time

`DebateConfig` can be set at the pipeline level and/or the stage level. Resolution in
`resolve_debate_config`:

- The enable flags (`enable_judge`, `enable_arbiter`) are OR'd across the global floor
  (`HIVEPILOT_ENABLE_DEBATE_JUDGE` / `HIVEPILOT_ENABLE_CHALLENGE_ARBITER`), the pipeline
  block, and the stage block. This is **strengthen-only** — a pipeline or stage block can
  turn a gate on even if the global floor has it off, but it can never turn off a gate the
  global floor has enabled.
- `runner`, `model`, and `confidence_threshold` resolve first-non-None in order
  `stage > pipeline > floor`.

#### `LessonsConfig` (pipeline-level only — no stage tier)

- `enable_distillation`
- `enable_semantic`
- `distill_runner`
- `distill_model`
- `min_score` — validated to be in `(0, 1]`
- `inject_limit` — validated to be `>= 1`

Same strengthen-only precedence over the global floor as `DebateConfig`, but `LessonsConfig`
only exists at the pipeline level — there is no per-stage override. Full mechanics (what the
judge/arbiter/distiller actually do, lesson injection) are in
[DEBATE-AND-LESSONS.md](DEBATE-AND-LESSONS.md); this section documents only the config
shape and precedence rule.

```yaml
pipelines:
  ship-feature:
    description: Plan, implement, and review a feature
    debate:
      enable_judge: true
      confidence_threshold: 0.7
    lessons:
      enable_distillation: true
      min_score: 0.6
      inject_limit: 5
    stages:
      - name: implement
        task: implement-feature
        debate:
          enable_arbiter: true
          confidence_threshold: 0.8
```

## policies.yaml — `Policy`

Top key is `policies:`. `default:` is merged with `projects.<name>:` to produce the
effective policy for a given project.

### `Policy`

- `allow_auto_git` (default `True`)
- `require_approval` (default `False`)
- `allow_containers` (default `True`)
- `role_overrides` — dict of role name → `{runner, model, effort, host}`
- `allowed_runners: list | None` — `None` means unconstrained; `[]` means deny-all
  (fail-closed)
- `secrets_fail_mode` — `"closed"` | `"fallback"` (default `"closed"`)
- `block_on_severity` — CVE severity gate, `None` by default (no gate)
- `scan_tool` — `"grype"` | `"osv-scanner"`

```yaml
policies:
  default:
    allow_auto_git: true
    require_approval: false
    allowed_runners: [claude, codex]
    secrets_fail_mode: closed
    block_on_severity: high
    scan_tool: grype

  projects:
    acme-api:
      require_approval: true
      role_overrides:
        developer:
          runner: claude
          model: claude-opus-4-1
          effort: high
      allowed_runners: [claude]
```

`allowed_runners: []` denies every runner for that scope — fail-closed, not "unset". A
security-scanner failure (not just a finding above `block_on_severity`) also blocks the run.
See [SECURITY.md](SECURITY.md) for the full fail-closed model.

## groups.yaml — `Group`

Defines multi-repo / monorepo groupings for coordinated pipeline runs across components.

### `Group`

- `description`
- `hub` — the project where group-level planning runs
- `components: list` — member projects
- `tags: dict` — tag name → list of component names; resolves a stage's `only_tags`
- `single_repo` (default `False`) — when `True`, execution happens once at the hub with no
  per-component fan-out; requires `hub` to be set

```yaml
groups:
  platform:
    description: Core platform services
    hub: acme-api
    components: [acme-api, acme-worker, acme-web]
    tags:
      backend: [acme-api, acme-worker]
      frontend: [acme-web]
    single_repo: false
```

## schedules.yaml — `ScheduleEntry`

Defines recurring task runs, executed by the scheduler daemon.

### `ScheduleEntry`

- `name`
- `task`
- `projects: list`
- `interval_minutes` (default `1440`)
- `enabled` (default `True`)

```yaml
schedules:
  - name: nightly-dependency-scan
    task: dependency-scan
    projects: [acme-api]
    interval_minutes: 1440
    enabled: true
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for running the scheduler daemon.

## model_profiles.yaml

A plain dict, not a list-of-models schema. Two top-level keys:

- `claude_profiles:` — profile name → `{model: ...}`
- `role_profiles:` — role name → profile name

```yaml
claude_profiles:
  fast:
    model: claude-haiku-4-5
  deep:
    model: claude-opus-4-1

role_profiles:
  developer: fast
  ciso: deep
```

## api_tokens.yaml — auth tokens

Not part of the config-repo sync set (kept local, per-deployment).

- `tokens:` — list of `{role, token_hash, note}`

Manage tokens exclusively via `hivepilot tokens add/list/rotate/remove` (see
[CLI-REFERENCE.md](CLI-REFERENCE.md)). Never hand-edit `token_hash` values in this file.

## Environment (`HIVEPILOT_` prefix)

Runtime settings are defined by a pydantic-settings `Settings` model with
`env_prefix="HIVEPILOT_"`. Values are loaded from a `.env` file, resolved through:

1. `HIVEPILOT_ENV_FILE` (explicit override)
2. `$XDG_CONFIG_HOME/hivepilot/.env`
3. `.env` in the current working directory

The repo ships an exhaustive `.env.example` with roughly 166 variables, every one commented
with its default value — use it as the reference rather than a full enumeration here.

File-path settings mirror each YAML file 1:1: `projects_file`, `tasks_file`, `roles_file`,
`pipelines_file`, `policies_file`, `groups_file`, `schedules_file`, `model_profiles_file`,
`api_tokens_file`, plus `state_db`, `runs_dir`, `prompts_dir`.

The debate/lessons **global floor** settings live here — a per-pipeline `debate:`/`lessons:`
block can only strengthen these, never weaken them:

- `enable_debate_judge`
- `judge_runner`
- `judge_model`
- `enable_challenge_arbiter`
- `judge_confidence_threshold`
- `enable_lesson_distillation`
- `lesson_distill_runner`
- `lesson_distill_model`
- `lesson_min_score`
- `lesson_inject_limit`
- `enable_semantic_lesson_retrieval`

```bash
# .env
HIVEPILOT_ENABLE_DEBATE_JUDGE=false
HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD=0.6
HIVEPILOT_ENABLE_LESSON_DISTILLATION=false
HIVEPILOT_LESSON_MIN_SCORE=0.5
HIVEPILOT_LESSON_INJECT_LIMIT=3
```

For the complete variable list, consult `.env.example` in the repo root.

## See also

- [CLI-REFERENCE.md](CLI-REFERENCE.md)
- [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md)
- [SECURITY.md](SECURITY.md)
- [RUNNERS.md](RUNNERS.md)
- [DEBATE-AND-LESSONS.md](DEBATE-AND-LESSONS.md)
