# Partitions: propose → ratify → dispatch

A **partition** splits one piece of work into N budgeted tasks, has a human
ratify the plan in the browser (or on the CLI), and then dispatches those
tasks as parallel runs with a durable journal recording what each one did.

The one idea worth the machinery: **running agents and publishing outward are
two different decisions.** Ratifying a plan says "this is the right work".
A separate, always-unticked consent says "and it may become visible outside
this machine". Both must be given.

## What is engine and what is your config

This distinction is the whole architecture, so it comes first.

| Concern | Engine | Your config |
|---|---|---|
| The partition JSON schema and its validation | ✅ | |
| The ratification gate, edit-then-approve, the audit trail | ✅ | |
| The outward-action vocabulary and consent enforcement | ✅ | |
| The wave planner, the concurrency cap, the claim/commit journal | ✅ | |
| **Who the proposer is** — its role, its prompt | | ✅ |
| **What a "task" means** for your organisation | | ✅ (the proposer's prompt) |
| Which sources exist beyond the four built-ins | | ✅ (a plugin) |
| Which pipelines a task is allowed to name | | ✅ (`pipelines.yaml` + `policies.yaml`) |

HivePilot ships **no** proposer role, **no** proposer prompt and **no**
opinion about what a task is. It ships the contract, the gate, the dispatch
and the journal. Everything on the right-hand column above lives in your own
config repo, and a different company with a different product gets this
feature with config changes only.

## A worked partition

This is a complete, valid document — every field the engine reads, with
nothing elided.

```json
{
  "partition_version": 1,
  "source": {
    "kind": "text",
    "ref": "docs/bug-1234.md",
    "digest": "sha256:9f2c1b0e5a4d3c2b1a09f8e7d6c5b4a39281706f5e4d3c2b1a0918273645566a"
  },
  "proposer": {
    "role": "partitioner",
    "pipeline": "propose-partition",
    "run_id": 4711,
    "generated_at": "2026-07-27T09:12:00Z"
  },
  "policy": {
    "max_parallel": 3,
    "on_task_failure": "continue"
  },
  "tasks": [
    {
      "id": "parse-guard",
      "title": "Guard the null deref in the parser",
      "project": "acme-api",
      "pipeline": "bugfix",
      "prompt": "The report in docs/bug-1234.md describes a crash in the header parser when Content-Length is absent. Write a failing repro test first, then guard the dereference.",
      "depends_on": [],
      "budget": { "wall_clock_seconds": 1500, "cost_usd": 1.50 },
      "done_when": ["the repro test fails before the change and passes after"],
      "outward": true
    },
    {
      "id": "parse-fuzz",
      "title": "Fuzz the header parser for neighbouring cases",
      "project": "acme-api",
      "pipeline": "bugfix",
      "prompt": "Add property-based coverage around the header parser for absent, empty and non-numeric Content-Length. Do not change parser behaviour in this task.",
      "depends_on": [],
      "budget": { "wall_clock_seconds": 1500, "cost_usd": 1.50 },
      "done_when": ["new cases are covered and the suite is green"],
      "outward": true
    },
    {
      "id": "release-note",
      "title": "Write the release note for the fix",
      "project": "acme-api/docs",
      "pipeline": "docs-refresh",
      "prompt": "Summarise the parser fix for the changelog. One paragraph, no marketing.",
      "depends_on": ["parse-guard"],
      "budget": { "wall_clock_seconds": 600, "cost_usd": 0.40 },
      "done_when": ["the changelog entry names the affected versions"],
      "outward": false
    }
  ]
}
```

### Field notes

- **`partition_version`** — `1`. The only accepted value today.
- **`source`** — provenance only. `kind` must resolve in the fail-closed
  source registry (`run`, `verdict`, `drift`, `text`, plus anything a plugin
  registers); `digest` lets the gate warn when the source moved under the
  plan.
- **`proposer`** — audit provenance. The engine records what the proposer
  claims; it does not authenticate it.
- **`policy.max_parallel`** — a request, not a promise. See
  [Effective parallelism](#effective-parallelism-is-not-max_parallel).
- **`policy.on_task_failure`** — `continue` (default) or `halt`.
- **`budget.wall_clock_seconds`** — **mandatory**, and the only budget the
  engine can hard-enforce. It maps to the step timeout, so it is presented
  everywhere as *"killed after N"*, never as an estimate.
- **`budget.cost_usd`** — **mandatory**, admission control. The partition
  **sum** is checked against the remaining daily budget and against
  `max_partition_cost_usd`, and re-checked at each wave boundary.
- **A `tokens` budget is rejected**, deliberately: it is unenforceable across
  the `ansible`/`kubectl`/`helm`/`shell`/`container` runners. Cost is
  derivable from tokens, not the reverse.
- **`depends_on`** — forms a DAG. Cycles, unknown ids and duplicate ids are
  rejected at load. An empty list means parallel-eligible; waves are the
  topological levels.
- **`done_when`** — prompt material and journal material for the human. It is
  **not** an assertion DSL. Engine-checkable "done" is the task's own
  pipeline reaching a terminal success — the pipeline already *is* the
  definition of done, and a second one would be config living in the engine.
- **`project`** — `<project>` or `<project>/<module>`, resolved by the
  ordinary project resolver.

## The example proposer — **this is config, not engine code**

Everything in this section belongs in **your config repo**. None of it ships
with HivePilot, none of it is special-cased by the engine, and you are meant
to replace it wholesale. A proposer is an ordinary role running an ordinary
pipeline whose last stage happens to shell out to `hivepilot partition
submit` — exactly the seam `hivepilot autopilot enqueue` already uses, and it
works with any runner.

`roles.yaml` — **config**:

```yaml
roles:
  - name: partitioner
    display_name: Partitioner
    prompt_file: prompts/agents/partitioner.md
    runner: claude
```

`tasks.yaml` — **config**:

```yaml
tasks:
  propose-partition:
    description: >
      Reads one work item and emits a partition document. Proposes only —
      it can never dispatch anything itself.
    steps:
      - name: draft the partition
        runner: claude
        role: partitioner
        prompt_file: prompts/agents/partitioner.md
      - name: submit it for ratification
        runner: shell
        command: >
          hivepilot partition submit
          --file plan.json
          --source text:docs/bug-1234.md
```

`pipelines.yaml` — **config**:

```yaml
pipelines:
  propose-partition:
    stages:
      - name: propose
        task: propose-partition
```

`prompts/agents/partitioner.md` — **config**. This prompt is where *"what a
task means here"* is actually defined. HivePilot has no opinion on it:

```markdown
You split one work item into independent tasks for other agents.

Rules:
- Emit ONLY a JSON document matching the partition contract, into plan.json.
- Every task names an existing project and an existing pipeline.
- Every task carries wall_clock_seconds (an enforcement ceiling — the task is
  killed at it) and cost_usd.
- Prefer independent tasks. Use depends_on only for a real ordering
  constraint, never for narrative order.
- Never propose a pipeline that merges a pull request.
```

Nothing above is validated by name, hard-coded, or discovered by the engine.
Swap `partitioner` for `triage-lead`, point it at Jira through a source
plugin, and redefine "task" to mean "one migration step" — the engine does not
change.

## Policy — what makes a project partition-capable

Three per-project keys in `policies.yaml`. All three are **fail-closed**:
absent, empty, non-numeric, zero or negative all mean *deny*, never
*unbounded*.

```yaml
policies:
  projects:
    acme-api:
      # ALLOWLIST of outward actions a ratified partition may ever be
      # consented to perform here. Absent or empty ⇒ nothing outward may
      # EVER be consented to, no matter what the operator ticks.
      outward_actions: [git_push, forge_pr]

      # Per-partition USD admission ceiling. Absent or <= 0 ⇒ no partition
      # may be ratified for this project at all.
      max_partition_cost_usd: 5.0

      # Per-task wall-clock ceiling in seconds. Absent or <= 0 ⇒ no
      # partition may be ratified for this project at all.
      max_task_wall_clock_seconds: 1800

      # Also required: the partition cost sum is checked against the
      # REMAINING daily budget as well as the ceiling above.
      budget_daily_usd: 20.0
```

A project with none of the first three keys is simply not partition-capable,
and that is not a misconfiguration — `hivepilot config doctor` says nothing
about it.

### `hivepilot config doctor`

Two checks exist for the two ways this setup silently does not do what it
looks like it does:

- **`partition_missing_cost_ceiling`** (error) — the project is
  partition-capable but has no positive `max_partition_cost_usd`, so the gate
  refuses every partition naming it, and the refusal only appears *after* a
  human has reviewed a plan and pressed dispatch.
- **`partition_parallelism_capped_at_one`** (warning) — see below.

### Effective parallelism is not `max_parallel`

`claude_max_concurrency` defaults to **1**. The runner throttle caps every
`claude`-kind runner at it, so on a default install a partition asking for
`max_parallel: 3` is *one agent three times* — the same work, serialised, with
wall-clock ceilings that were sized for the parallel case.

The ratification view shows the **computed effective number**, not the
requested one, and `config doctor` warns before you ever write a plan around
a parallelism this host will not deliver. Raise
`HIVEPILOT_CLAUDE_MAX_CONCURRENCY`, or plan with `max_parallel: 1` so the
document states what actually happens.

## Outward-action consent

Outward is a **different axis from destructive**. Destructive asks "could this
damage the target system", is per-step, and is resolved by the runner at
execution time. Outward asks "may this become visible outside this machine",
is per-dispatch, and is resolved by the operator at ratification.
`terraform apply` is destructive and not outward; `gh pr create` is outward
and not destructive. Both must be satisfied.

| Token | What it covers |
|---|---|
| `git_push` | pushing a branch |
| `forge_pr` | opening or commenting on a pull request |
| `forge_merge` | merging a pull request — **always refused** in a partition dispatch |
| `forge_issue` / `forge_release` | opening an issue / publishing a release |
| `notify` | outbound Telegram / Slack / Discord / Signal |
| `vault_write` | writing notes to your vault |
| `external_api` | plugins declaring the `network` capability |

The footprint is computed from **live pipeline config**, never from the
plan's own `outward` flags — the JSON box an operator edits must not be a
privilege-escalation surface. The consent checkbox defaults to unticked and
names the exact actions it is asking about.

### How consent is enforced

Two layers, both fail-closed.

**Admission**, at ratify: the computed footprint must be a subset of the
project's `outward_actions` allowlist, `merge_pr` is refused
unconditionally, and a non-empty footprint without consent is denied by
name.

**Runtime**, at dispatch: each task's run is given a *run-scoped outward
permission* — the set of tokens it may act on — alongside the existing
`auto_git` flag. Every token in the table above now has runtime teeth:

| Token | Where it is enforced at runtime |
|---|---|
| `git_push` / `forge_*` | the existing `auto_git` suppressor |
| `notify` | `notification_service` outbound: plain notifications, the event webhook, agent live-streams, and approval keyboards |
| `vault_write` | every `ObsidianService` write, plus the `commits_vault` stage branch |
| `external_api` | resolving a runner, or running a lifecycle hook, contributed by a plugin that declared the `network` capability |

The permission granted by a ticked checkbox is **exactly the project's
`outward_actions` allowlist**, minus `forge_merge`. The allowlist is the
ceiling consent is measured against, not a starting point — so a project
that allowlists only `git_push` and `forge_pr` will still have its
partition-dispatched notifications and vault writes suppressed even with
consent ticked. Add the tokens you want to the allowlist.

An unresolvable policy grants **nothing**: "I cannot tell what this project
may do" never resolves to "it may do anything".

**Suppression is never silent.** Every suppressed action is logged
(`outward.suppressed`, naming the action and the engine surface), and a
per-task summary is written to the `interactions` audit
(`partition.outward_suppressed`). If a partition run sends you no Telegram
message, that is recorded as a decision, not lost as a failure.

**Only partition-dispatched runs are constrained.** An ordinary
`hivepilot run`, a scheduled run and an API run open no permission scope at
all, so every gate short-circuits to "permitted" and their behaviour is
unchanged.

**The remaining honest gap:** an agent with shell access can `git push`,
`curl` an API or write to your vault regardless of any flag. This gate
governs what the *engine* does — the same honesty the plugin capability
manifest states about itself. Suppression also stops at the process
boundary: a runner subprocess that opens its own socket is not intercepted.

## Dispatch

Ratified tasks are written into the existing autopilot queue under
`kind='partition_task'`, so they reuse its atomic claim, its pause/stop kill
switch, and its visibility — and the ordinary autopilot drain never picks
them up.

Tasks run in **waves** (topological levels), each capped at
`min(policy.max_parallel, concurrency_limit, runner cap)`. Before each wave
the pause state and today's spend are re-checked. Each task is **claimed
before its run row is created**, so a crash leaves a visible claimed row with
no run — recoverable, and never a double dispatch.

- A failed task's dependents are marked **`skipped`, never `failed`**:
  running a task whose prerequisite failed is a correctness bug, not a policy
  choice.
- Independent siblings follow `policy.on_task_failure`. `halt` additionally
  requests cancellation of not-yet-started tasks. **Running agents are never
  killed** — cancellation is cooperative.
- A retry creates a **new run** under the same `(partition, task)` with
  `attempt + 1`. **A ratified partition is immutable** — that is what makes
  the audit meaningful. Wanting a different plan means proposing a new one.
- The cost ceiling is a soft pre-check, not a reservation. Within one wave
  nothing is locked, so the ceiling can be overshot by up to one wave's
  declared budget.

## The journal

`partition_tasks` holds one durable row per `(partition, task)`: status,
queue id, run id, attempt, who claimed it and when, the PR URL, the measured
cost and the wall-clock ceiling. Statuses are `pending`, `claimed`,
`running`, `committed`, `failed`, `skipped`, `cancelled`.

Pollen renders it under **Partitions → Journal**. Its contract is honesty:

- The status shown is the token the engine stored, not a re-worded label.
- **A missing PR link renders `—`, never a guessed URL.** The engine
  attributes a URL only when *exactly one* pull request was opened inside a
  task's window, so two tasks running against the same project at once are
  both recorded as `—`. A missing link is a gap; a wrong link would be a lie.
- A cost that was never measured on a finished task reads **`unknown`**, not
  `$0.00`. `—` and `unknown` mean different things and are never
  interchanged: `—` is "not recorded", `unknown` is "recorded as
  unmeasurable".

## CLI

```
hivepilot partition submit --file plan.json --source <kind>:<ref> [--tenant default]
                           [--proposer-run-id N] [--allow-source-drift]
hivepilot partition show   <id> [--plan] [--tenant default]
hivepilot partition ratify <id> --approver <who> [--file edited.json]
                                [--consent] [--expected-digest <sha256:...>]
hivepilot partition status <id> [--tenant default]
hivepilot partition cancel <id> --actor <who> [--tenant default]
```

`--approver` and `--consent` are deliberately two flags: ratification says
"this is the right work", consent says "publish it". `--consent` defaults to
off, so an outward-acting pipeline is refused unless you say so explicitly.

## API

| Endpoint | Rank |
|---|---|
| `GET /v1/partitions` | `run` |
| `GET /v1/partitions/{id}` | `run` |
| `POST /v1/partitions/{id}/preview` | `approve` |
| `POST /v1/partitions/{id}/ratify` | `approve` |
| `POST /v1/partitions/{id}/cancel` | `run` |

The two reads sit at `run` rather than `read` because a partition document
carries every task's full prompt. All of them are tenant-scoped.

## See also

- [AUTOPILOT.md](AUTOPILOT.md) — the queue, the budget and the kill switch a
  partition dispatch reuses.
- [CONFIGURATION.md](CONFIGURATION.md) — the full `policies.yaml` schema.
- [DASHBOARD.md](DASHBOARD.md) — Pollen, where ratification happens.
