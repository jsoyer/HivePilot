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
- `claude_md` — path to a project-specific CLAUDE.md-style instructions file. Its
  **content** (not just the path) is resolved and inlined into the agent prompt — see
  `instruction_files` below for the full behavior, which `claude_md` shares.
- `instruction_files: list[str]` — additional repository instruction/context files (e.g.
  `AGENTS.md`, `.cursorrules`, `.windsurfrules`, `GEMINI.md`) whose content is inlined into
  the prompt alongside `claude_md`'s, in declared order, each clearly delimited. Each entry
  resolves relative to the project's `path` (an absolute entry, or one starting with `../`,
  is honored as-is — HivePilot never walks parent directories on its own; you state the
  relationship explicitly, which also covers the monorepo/umbrella pattern where shared
  instructions live one level above the product repo). A declared file that can't be found
  never fails silently: it logs a warning and injects a visible "NOT FOUND" notice naming the
  file and the directory searched, so a broken reference is impossible to miss. Size is capped
  per-file and in total (`HIVEPILOT_INSTRUCTION_FILE_MAX_BYTES` / `HIVEPILOT_INSTRUCTION_FILES_MAX_TOTAL_BYTES`,
  default 20000 / 60000 bytes) with an explicit truncation notice when a cap is hit, and
  content is redacted through the same secret-masking sink as every other runner output.
- `default_branch` (default `"main"`)
- `owner_repo` — `owner/repo` slug, used for PR/git-host operations
- `env: dict[str, str]` — environment variables injected into runs for this project
- `secrets: dict[str, dict]` — maps `NAME` to a secret spec (`{source, ...}`); referenced
  in `env` values as `${secret:NAME}` and resolved at run time (see
  [SECURITY.md](SECURITY.md))
- `obsidian_vault` — where **this project's** HivePilot artifacts are written (and read
  back from). Overrides the global `HIVEPILOT_OBSIDIAN_VAULT`. See
  [Per-project Obsidian vault](#per-project-obsidian-vault--obsidian_vault) below.

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
      # KMS envelope-encrypted secret (see SECURITY.md → KMS backend):
      DB_PASSWORD:
        source: kms
        provider: aws            # or HIVEPILOT_KMS_PROVIDER
        encrypted_data_key: "<base64>"
        ciphertext: "<base64>"   # AES-GCM ciphertext (tag appended, or add `tag:`)
        iv: "<base64>"
      # 1Password (Connect via op_connect_host, OR direct service-account when
      # only HIVEPILOT_OP_SERVICE_ACCOUNT_TOKEN is set):
      API_KEY:
        source: onepassword
        ref: "op://Prod/acme-api/api-key"
    env:
      GITHUB_TOKEN: "${secret:GITHUB_TOKEN}"
```

Secrets-related environment settings (`HIVEPILOT_` prefix): `KMS_PROVIDER` /
`KMS_KEY_ID` (KMS backend), `OP_INTEGRATION_NAME` / `OP_INTEGRATION_VERSION`
(1Password direct mode), and `SECRETS_CACHE_TTL_SECONDS` — the opt-in in-memory
secret cache TTL (default `0` = disabled; flush with `hivepilot secrets
cache-clear`). See [SECURITY.md](SECURITY.md).

### Per-project Obsidian vault — `obsidian_vault:`

`HIVEPILOT_OBSIDIAN_VAULT` is a single machine-wide path, so by default every project's
artifacts land in the same vault. But HivePilot's unit of configuration is a *pipeline*,
and several pipelines routinely coexist on one host — your own HivePilot work and a
product pipeline want different vaults. `obsidian_vault:` on a project makes the
destination a per-project decision:

```yaml
projects:
  hivepilot:
    path: /home/jerome/code/hivepilot
    # HivePilot's own work -> the personal vault
    obsidian_vault: /home/jerome/vaults/personal

  noxys:
    path: /home/jerome/code/noxys
    # the product pipeline's work -> the project vault
    obsidian_vault: /home/jerome/vaults/noxys

  legacy:
    path: /home/jerome/code/legacy
    # no key -> inherits HIVEPILOT_OBSIDIAN_VAULT, exactly as before
```

It applies to **every** vault destination for that project: per-stage run artifacts and
canonical `02 - Artifacts/<role>/` deliverables, the Obsidian plugin's daily journal and
step outcomes, debate ADRs, auditor notes, interaction logs, and the `{OBSIDIAN_VAULT}`
prompt variable the agent itself sees. The plugin's `recall` (read) resolves through the
same function as its `store` (write), so a project never recalls context from a vault it
does not write to.

Rules — all deliberate, all fail-closed:

| Value | Behaviour |
| --- | --- |
| key absent (or `obsidian_vault: null`) | inherit the global `HIVEPILOT_OBSIDIAN_VAULT` — byte-identical to a deployment that predates this field |
| absolute path (`~` is expanded) | used for this project |
| **empty / whitespace-only** | **projects.yaml refuses to load.** Empty is never read as "use the global": an empty value is always a mistake (a typo, or an unexpanded `${VAULT}`), and silently falling back would route this project's artifacts into the very vault you were moving away from |
| **relative path** | **projects.yaml refuses to load.** A relative vault path resolves against whatever working directory the daemon has, so the same config would write to different places depending on how HivePilot was started |
| absolute path, directory does not exist | the run fails loudly, before any stage executes. HivePilot **never creates a vault directory** — a silently created vault is how artifacts end up somewhere nobody looks. Create it yourself (`mkdir -p`), ideally as a git repo |

One more fail-closed rule: a single run over **several** projects whose vaults differ is
rejected up front. A run writes one aggregated artifact per stage, so there is exactly one
destination; picking one project's vault would file another project's work in it. Give
every project in the run the same `obsidian_vault:`, or run them as separate pipelines.

`hivepilot config doctor` reports projects that share the global vault and, once any
project declares an override, stops reporting it.

### Monorepo modules — `modules:` / `ui_surfaces:` and `project/module` targeting

A project can declare `modules`: a map of module name → subpath (relative to `path`) inside
the repo. A run can then target `<project>/<module>` to scope the agent's working directory
to that subdirectory instead of the repo root — a single `git clone` (via `owner_repo` /
`ensure_checkout`, unchanged) is shared across every module target.

- `modules: dict[str, str]` (default `{}`) — module name → subpath relative to `path`.
  Validated at load time: a subpath must resolve to a location *inside* `path` — an absolute
  path or a `..`-escaping subpath (e.g. `../../etc`) is rejected outright (fails to load)
  rather than silently scoping an agent outside the repo checkout.
- `ui_surfaces: list[str]` (default `[]`) — module names (a subset of `modules`' keys) that
  are UI surfaces; pure metadata for callers, not consumed by run dispatch itself.

```yaml
projects:
  noxys:
    path: /home/jerome/code/noxys
    owner_repo: acme-org/noxys
    modules:
      detection-core: apps/detection-core
      adjudication: apps/workers/adjudication
    ui_surfaces:
      - detection-core
```

```bash
# Scoped to apps/detection-core/ — the agent's working directory and any
# git diff/commit actions all operate inside the subdirectory.
hivepilot run noxys/detection-core ship

# Unscoped — repo root, byte-identical to a project with no `modules` declared.
hivepilot run noxys ship
```

Target resolution order: an exact project name always wins first; then a `<project>/<module>`
split (the module is looked up in that project's `modules` map — an unknown module fails with
a clear error listing the available ones); then a group name (`groups.yaml`). A project with
no `modules` configured plus a `/module` target also fails with a clear, actionable error.

Cross-module work (a change that spans multiple modules, or any repo-wide operation such as a
release, a full-repo scan, or a PR) still targets the **project**, not a module — run
`hivepilot run noxys <task>` for anything that isn't scoped to one module's subtree. Module
targeting is a working-directory scope for the agent, not a build/dependency graph.

This first-class monorepo-modules feature replaces the older workaround pattern of declaring
one sibling `ProjectConfig` per module (each pointed at the same clone) plus a `single_repo:
true` group (see `groups.yaml` → `Group` below) purely to fan a pipeline out over subtrees of
one repo. Projects that don't declare `modules` are entirely unaffected — every field is
additive and defaults to empty.

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
- `pr_body_file` — path (relative to the project's own repo, or absolute) to
  a file an agent step is expected to have written as this stage's PR
  description. The engine itself never creates this file. If it's absent or
  blank when the PR is opened, HivePilot logs a warning and falls back to a
  redacted, size-capped body built from the stage's own output (then
  `pr_title`/`commit_message`, then a generic message) instead of failing
  the PR — see `hivepilot/services/pr_body.py`.
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

### `cross_cutting_rules` — the policy floor every role inherits

An optional **top-level** key of `roles.yaml` (a sibling of `roles:`, not a field of a
role). It holds short natural-language policy statements that every role inherits, on top
of its own rule-source file paths:

```yaml
cross_cutting_rules:
  - "All artifacts must be written in English (no other language)."
  - "Never merge to main without a passing review."

roles:
  - name: developer
    # ...
```

HivePilot is a generic orchestrator, so the **engine default carries no
organisation-, jurisdiction-, language- or tooling-specific statement** — a deployment
that is not EU-based, works in another language, or runs a different toolchain must not
have to opt *out* of someone else's policy. Only one statement ships by default:

```
Privacy-by-design: never log or surface raw prompt content.
```

Anything beyond that is yours to declare. Configuration **replaces** the default, it does
not merge with it, so you can retire that statement too.

**Absent and empty do not mean the same thing** — this is deliberate:

| `roles.yaml` contains          | Resolved rules      | Why |
| ------------------------------ | ------------------- | --- |
| no file / no key               | engine default      | Silence means "no opinion expressed". A missing file must never silently delete a policy floor you believed was in force. |
| `cross_cutting_rules:` (empty) | engine default + warning | A bare key with nothing under it parses as `null` — far more likely a half-finished edit than a decision. Write `[]` if you mean it. |
| `cross_cutting_rules: []`      | *(none)*            | An organisation with genuinely no cross-cutting rules is legitimate, and this is a deliberate, reviewable, diffable statement rather than a silence. |
| anything not a list of strings | engine default + warning | A typo must never leave you with *fewer* rules than the engine ships. |

A role that is not present in the rules registry at all still inherits the resolved
floor rather than an empty manifest.

### `vault_rule_documents` — canonical rule documents in your vault

Another optional **top-level** key of `roles.yaml`. It maps a generic *slot* to the
*filename* your Obsidian vault uses for that document. Each resolved document is
injected by path into the rule manifest of the roles that need it:

```yaml
vault_rule_documents:
  security_rules: "AGENT-SECURITY-RULES.md"
  git_branch_rules: "AGENT-GIT-BRANCH-RULES.md"
```

| Slot               | Roles that receive it                             |
| ------------------ | ------------------------------------------------- |
| `security_rules`   | `ciso`, `developer`, `reviewer`, `qa`, `documentation` |
| `git_branch_rules` | `cto` and every coding role                       |

Paths are built as `<obsidian_vault>/08 - Security/<filename>`, so this key does
nothing unless `HIVEPILOT_OBSIDIAN_VAULT` points at an **absolute** path. Filenames
must be **bare names** — a value containing `/`, `\`, `.` or `..` is rejected with a
warning, because config here names a document *inside* the vault and must not become
an arbitrary-path read for every agent.

**The engine ships no document names at all.** It cannot know what your vault calls
its security document, and a guessed filename resolves to a nonexistent absolute path
that an agent is then instructed to read — it either errors or invents the contents.
This is the opposite default from `cross_cutting_rules` above, on purpose: a rule
*statement* is self-contained text that is safe to ship, while a rule *document path*
is only meaningful if the file is really there.

| `roles.yaml` contains            | Resolved documents | Why |
| -------------------------------- | ------------------ | --- |
| no file / no key / bare null key | *(none)*           | The engine has no correct filename to guess. If `obsidian_vault` **is** configured, this logs `vault_rule_documents_absent` — silence with a vault set is the one case an operator can get wrong. |
| an unknown slot name             | that entry ignored + warning | A typo'd slot silently yielding "no document" is the empty-means-no-constraint failure, so it is named in the log. |
| a blank or whitespace-only value | that slot dropped + warning | Never a path ending in `/`. |
| a value with a path separator or `..` | that slot dropped + warning | Config names a document in the vault, not anywhere on disk. |
| anything not a mapping of strings | *(none)* + warning | |

Resolution is **per slot**: an omitted slot resolves to `""` and drops out of the
manifest. Because every engine default is empty there is nothing to merge with, so
declaring this key *adds* documents rather than replacing a shipped set — unlike
`cross_cutting_rules`, which **replaces** a non-empty default.

For backward compatibility the module-level constants `VAULT_SECURITY_RULES`,
`VAULT_GIT_BRANCH_RULES` and the deprecated alias `VAULT_DETECTION_FABRIC` remain
plain strings bound at import time; only the *source* of the filename moved to config.

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
- `reviewers` — list of reviewer role names
- `review_target` — `"internal"` or `"github_pr"`; requires at least one
  resolved `reviewers` entry (fail-closed: `review_target` set with an
  empty resolved `reviewers` list raises — at YAML-load time within one
  `debate:` block, or at pipeline load via `validate_pipeline` when the
  empty-reviewers case only appears once pipeline- and stage-level blocks
  are combined)

`DebateConfig` can be set at the pipeline level and/or the stage level. Resolution in
`resolve_debate_config`:

- The enable flags (`enable_judge`, `enable_arbiter`) are OR'd across the global floor
  (`HIVEPILOT_ENABLE_DEBATE_JUDGE` / `HIVEPILOT_ENABLE_CHALLENGE_ARBITER`), the pipeline
  block, and the stage block. This is **strengthen-only** — a pipeline or stage block can
  turn a gate on even if the global floor has it off, but it can never turn off a gate the
  global floor has enabled.
- `runner`, `model`, and `confidence_threshold` resolve first-non-None in order
  `stage > pipeline > floor`.
- `reviewers`/`review_target` resolve `stage > pipeline > unset` (no global floor tier).
  `review_target="github_pr"` gates `promote_pr`/`merge_pr` in `perform_git_actions`,
  the same as `enable_judge`/`enable_arbiter`. `review_target="internal"` has no PR to
  gate — a blocking review verdict instead halts stage/pipeline progression directly.

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
      reviewers: [reviewer]
      review_target: github_pr
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

### Autopilot and partition fields

Resolved by `hivepilot/services/autopilot_policy.py` from the same
`default` → `projects.<name>` merge. Every one of them is fail-closed:
absent, empty, non-numeric, zero and negative all mean **deny**, never
"unconstrained".

- `auto_dispatch: list` — pipelines autopilot may dispatch unattended.
  Absent/empty ⇒ nothing auto-dispatches ([AUTOPILOT.md](AUTOPILOT.md))
- `budget_daily_usd: float | None` — positive daily spend ceiling. Absent or
  `<= 0` ⇒ no auto-dispatch and no partition
- `outward_actions: list` — **allowlist** of outward tokens a ratified
  partition may be consented to perform (`git_push`, `forge_pr`,
  `forge_merge`, `forge_issue`, `forge_release`, `notify`, `vault_write`,
  `external_api`). Absent or `[]` ⇒ **nothing outward may ever be consented
  to** for that project. A ticked checkbox can never authorise what policy
  never allowed
- `max_partition_cost_usd: float | None` — per-partition USD admission
  ceiling. Absent or `<= 0` ⇒ no partition may be ratified for that project
- `max_task_wall_clock_seconds: int | None` — per-task wall-clock ceiling.
  Absent or `<= 0` ⇒ no partition may be ratified for that project

```yaml
policies:
  projects:
    acme-api:
      auto_dispatch: [docs-refresh]
      budget_daily_usd: 20.0
      outward_actions: [git_push, forge_pr]
      max_partition_cost_usd: 5.0
      max_task_wall_clock_seconds: 1800
```

A project with none of the three partition keys is simply not
partition-capable — that is not a misconfiguration and `hivepilot config
doctor` says nothing about it. A project that sets one of them but has no
positive `max_partition_cost_usd` **is** reported (`config doctor` check
`partition_missing_cost_ceiling`), because the gate then refuses every
partition naming it and the refusal only surfaces after a human has reviewed
a plan. See [PARTITIONS.md](PARTITIONS.md).

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
- [PARTITIONS.md](PARTITIONS.md)
