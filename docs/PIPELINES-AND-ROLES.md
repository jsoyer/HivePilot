# Pipelines and Roles

HivePilot runs a repo through a **pipeline** — an ordered list of **stages**. Each stage runs a **task** bound to a **role**. A role resolves to a **runner + model + effort**. Composed together, this lets you model a software "company" of specialised agents: a stage where a developer role writes code, a stage where a reviewer role checks it, a stage where a CISO role gates a release, and so on.

This document explains the conceptual model. For exact field-by-field schemas, see [CONFIGURATION.md](./CONFIGURATION.md).

## The company model

A **role** is a named agent persona defined in `roles.yaml`. It carries:

- a prompt (what the agent is told to do)
- a keyed inputs/outputs contract (what data it consumes from prior stages, and what it produces for later ones)
- an order (where it typically sits in a pipeline)
- whether it `can_block` (whether a negative/blocking verdict from this role can halt the run)

The engine ships with **one generic role by default**: `developer → claude`. That is the only role HivePilot assumes exists.

The full multi-role roster — CEO → Chief of Staff → CTO → Developer → Reviewer → CISO → QA → Documentation → Report → Approval — is a **config-owned, opt-in template** (`examples/roles.yaml`). It's illustrative of what you can build, not something the engine hardcodes or requires. You add roles by editing `roles.yaml`; nothing in the orchestrator assumes a fixed org chart.

## Roles

Key `Role` fields:

- `name` — the identifier stages bind to
- `title` — human-readable label (e.g. "Chief Information Security Officer")
- `prompt_file` — path to the role's system prompt
- `model_profile` — named model profile the role defaults to
- `inputs` / `outputs` — the keyed context contract: a stage declares which prior stages' keyed outputs it consumes, and which key(s) it publishes for downstream stages to consume
- `can_block` — whether this role's verdict can block the pipeline (e.g. a CISO or reviewer role that fails a run)
- `order` — default position in a company-style pipeline
- `runner` — which runner plugin executes this role (see [RUNNERS.md](./RUNNERS.md))
- `model` / `models` — model id(s) the role uses
- `effort` — reasoning/thinking effort level
- `host` — optional host/environment constraint
- `command_task` — optional fixed command/task binding for non-conversational roles

Inputs/outputs are what make multi-stage pipelines composable without stages re-deriving context: a QA stage can consume the developer stage's `diff` output key and the CTO stage's `plan` output key without re-reading the whole run history.

Full field reference: [CONFIGURATION.md#rolesyaml](./CONFIGURATION.md#rolesyaml).

## Runner + model + effort resolution

Every stage ultimately needs a concrete runner, model, and effort level to execute. The orchestrator resolves these through **one precedence chain, applied in order**:

```
policy.role_overrides  >  stage  >  role  >  runner-default
```

Resolution steps:

1. The **role** binds a base runner, model, and effort (from `roles.yaml`).
2. A **pipeline or stage** `model`/`effort` override, if set, overrides the role's default for that stage only.
3. **Policy** (`role_overrides`, plus the `allowed_runners` gate) is applied **last**, as the top security control — this is deliberate: a pipeline/stage author cannot use a stage-level override to escape a policy constraint. Policy always wins.

`effort` is a single closed enum: `low | medium | high | xhigh | max`. There is no free-form effort value — an unrecognized value fails closed rather than silently falling back.

`mode` (`cli | api`) resolves independently, with its own precedence: `stage > pipeline > "cli"` (the built-in default).

See [RUNNERS.md](./RUNNERS.md) for what each runner plugin supports (CLI vs API mode, supported models, host requirements).

## Pipelines & stages

A `PipelineConfig` (in `pipelines.yaml`) has:

- `description`
- `mode` — pipeline-level default (`cli`/`api`)
- `model` / `effort` — pipeline-level defaults
- `stages` — the ordered list of `PipelineStage`
- `debate` — optional debate/judge configuration (see [DEBATE-AND-LESSONS.md](./DEBATE-AND-LESSONS.md))
- `lessons` — optional lessons-injection configuration

A `PipelineStage` has:

- `name`, `task` — what runs
- `mode` / `model` / `effort` — stage-level overrides (see precedence above)
- `pause_before` — pause the run before this stage for human plan approval
- `commits_vault` — whether this stage's output commits to the vault
- `only_components` / `only_tags` — scope this stage to a subset of a group's components
- `continue_on_failure` — don't fail-fast the run if this stage fails
- `skills` — skill(s) attached to this stage
- `debate` — per-stage debate override

Example `pipelines.yaml`:

```yaml
company-release:
  description: "Plan, implement, review, and document a release."
  mode: cli
  stages:
    - name: plan
      task: cto.plan_release
      pause_before: true        # human approves the plan before work starts

    - name: implement
      task: developer.implement

    - name: review
      task: reviewer.review_pr
      continue_on_failure: false

    - name: docs
      task: documentation.write_docs
      commits_vault: true        # docs output is committed to the vault
```

`pause_before: true` halts the run before that stage executes and waits for a human to approve the plan (see [Approvals](#approvals-in-a-pipeline) below). `continue_on_failure` lets a stage fail without aborting the rest of the run — useful for optional/advisory stages. `only_components` / `only_tags` restrict a stage to a subset of a group's member projects (see [Multi-repo groups](#multi-repo-groups)).

## Running a pipeline

```bash
# Dry-run is the default — shows what would happen, doesn't execute
hivepilot run-pipeline <project|group> <pipeline>

# Execute for real
hivepilot run-pipeline <project|group> <pipeline> --no-dry-run

# List available pipelines
hivepilot list-pipelines
```

`run-pipeline` also accepts an interactive mode for stepping through stages manually. `run` (single-task form) supports `--simulate`.

Any task that mutates git state runs in an **isolated git worktree**, not the caller's working tree — this keeps concurrent/parallel stages from corrupting a shared checkout.

## Multi-repo groups

`groups.yaml` defines a `Group`:

- `hub` — the project where group-level planning (e.g. a CTO/plan stage) runs
- `components` — the member projects in the group
- `tags` — a tag → components mapping, used to resolve a stage's `only_tags`
- `single_repo` — monorepo mode: a single execution runs at the hub with no fan-out across components; requires `hub` to be set

Running a pipeline against a group normally **fans out** — each stage runs once per component (subject to `only_components`/`only_tags` scoping) — unless `single_repo: true`, in which case everything executes once at the hub.

Example:

```yaml
platform-group:
  hub: platform-hub
  components:
    - api-service
    - web-frontend
    - worker-jobs
  tags:
    backend: [api-service, worker-jobs]
    frontend: [web-frontend]
```

A stage scoped to the `backend` tag:

```yaml
stages:
  - name: backend-migration
    task: developer.run_migration
    only_tags: [backend]   # only api-service and worker-jobs run this stage
```

## Approvals in a pipeline

Three gate levels, plus destructive-op auto-gating:

1. **Policy** `require_approval` — global policy requires approval for matching runs.
2. **Stage** `pause_before` — pauses the pipeline before a specific stage until approved.
3. **Step** `require_approval` — approval required for an individual step within a task.

In addition, steps recognised as **destructive operations** are auto-gated for approval regardless of the above settings.

While paused, use:

```bash
hivepilot approvals list
hivepilot approvals approve <id>
hivepilot approvals deny <id>
```

See [SECURITY.md](./SECURITY.md) for the full approval/policy model.

## Code review & PRs

A typical company pipeline has the developer role open a pull request and a reviewer role review it. Git actions — `create_pr`, `draft`, `promote_pr`, `merge_pr` — live on a task's `git:` block, not on the role or stage directly.

Optionally, an opt-in **debate judge/arbiter** can fail-closed gate `promote_pr`/`merge_pr` — if the debate verdict doesn't clear the configured confidence floor, the promote/merge is blocked rather than allowed through. See [DEBATE-AND-LESSONS.md](./DEBATE-AND-LESSONS.md).

## The default company roster

The engine ships a single generic `developer → claude` role by default. The
full multi-agent company below is the opt-in roster shipped in `roles.yaml` /
`examples/roles.yaml`. Each agent is a named role bound to a runner and one or
more models, fully overridable per project via a policy `role_overrides` entry.

| Agent | Role | Runner | Model(s) |
| --- | --- | --- | --- |
| Aliénor | `ceo` | `opencode` | `opencode-go/qwen3.7-max`, `opencode-go/kimi-k2.6` |
| Jules | `chief_of_staff` | `cursor` | runner default |
| Blaise | `cto` | `opencode` | `opencode-go/kimi-k2.7-code` |
| Gustave | `developer` | `claude` | runner default |
| Victor | `reviewer` | `codex` | `gpt-5.5` |
| Hugo | `ciso` | `opencode` | `opencode-go/glm-5.2` |
| Marie | `qa` | `cursor` | runner default |
| Théo | `documentation` | `gemini` | runner default |

**Henri** is a meta-agent: an independent **auditor** that reviews a run rather
than participating in the delivery chain. It is documented here for completeness
but is not one of the pipeline roles above.

## See also

- [CONFIGURATION.md](./CONFIGURATION.md) — full field reference for `roles.yaml`, `pipelines.yaml`, `groups.yaml`, policy
- [RUNNERS.md](./RUNNERS.md) — runner plugins, CLI vs API mode, model support
- [DEBATE-AND-LESSONS.md](./DEBATE-AND-LESSONS.md) — debate judge/arbiter and the lessons-injection loop
- [SECURITY.md](./SECURITY.md) — approval gates, policy, destructive-op auto-gating
- [CLI-REFERENCE.md](./CLI-REFERENCE.md) — full command reference
