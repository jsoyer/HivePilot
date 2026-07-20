# Autopilot: guarded objective queue + dynamic schedule

> **Autopilot is a bounded driver, NOT an unattended software factory.**
> It never decides *what* work matters — a human-authored "groomer" pipeline
> does that, as ordinary config. Autopilot's only job is to drain a small,
> explicitly-allowlisted, budget-capped queue of proposals, one at a time,
> through a fail-closed gate that can only ever say no by default.

## The flow

HivePilot already runs arbitrary pipelines on a fixed schedule
(`hivepilot/services/schedule_service.py`) and already performs gated
autonomous action for infrastructure drift remediation
(`hivepilot/services/drift_service.py` + `drift_schedule.py`). Autopilot
reuses both patterns for a third case: letting a pipeline decide what runs
next, instead of a human hard-coding a fixed `task:` in `schedules.yaml`.

```
 groomer pipeline (USER config)          engine (this feature)
 ───────────────────────────────         ─────────────────────────────
 runs on its own schedule, like    ─▶     autopilot_queue.enqueue(...)
 any other pipeline. Its last              -> row state: "proposed"
 step is a shell/CLI runner that
 calls:
   hivepilot autopilot enqueue \
     <pipeline> <project> \
     --reason "..."

 a human reviews the queue         ─▶     hivepilot autopilot promote <id>
 (`hivepilot autopilot queue`)              -> row state: "queued"

 a `source: autopilot` schedule    ─▶     schedule_service.run_entry()
 entry ticks (same due-calc as              -> autopilot_queue.drain_one()
 every other schedule)                        -> next_dispatchable()
                                               -> autopilot_gate()
                                               -> ALLOW: orchestrator
                                                  .run_pipeline(...)
                                                  -> mark "done"/"blocked"
                                               -> DENY: row stays exactly
                                                  as-is, reason logged
                                                  (Mirador can show
                                                  "awaiting human")
```

At most **one** objective is dispatched per schedule tick, regardless of
how many rows are queued.

## Queue lifecycle

```
proposed --(human `promote`)--> queued --(gate ALLOW)--> running --> done
   |                               |                                   |
   +--------- veto -------------->+------------ veto ----------------->+ vetoed
                                   |
                                (gate DENY, run_pipeline fails) --------> blocked
```

- **proposed** — a groomer pipeline (or a human via the CLI) proposed this
  objective. Never dispatched from here unless the gate independently
  allows it (see below) — promotion is not required for dispatch, but a
  human `promote` is the normal path for anything not already
  allowlisted+budgeted for full automation.
- **queued** — ready to be picked up by the next drain tick.
- **running** — a dispatch is in flight.
- **done** — the pipeline ran; cost is recorded.
- **blocked** — dispatch was attempted (gate ALLOW) but `run_pipeline`
  raised; the row is left visible for a human to investigate, never
  silently retried.
- **vetoed** — a human explicitly rejected the proposal.

## `policies.yaml` schema

Two new, **optional**, **disabled-by-default** fields, resolved per project
by `hivepilot/services/autopilot_policy.py` (default → project-override
merge, exactly like every other policy field):

```yaml
policies:
  default:
    require_approval: false

  projects:
    acme-api:
      # Explicit allowlist of pipeline names Autopilot may dispatch
      # unattended for this project. Absent or empty ⇒ nothing may ever
      # auto-dispatch for this project (the drain can still *propose*,
      # it just never advances proposals past "proposed" via the gate).
      auto_dispatch:
        - groomer-pipeline

      # Positive daily USD spend ceiling. Absent, null, or <= 0 ⇒ disabled
      # (no budget configured means no auto-dispatch, never an unbounded
      # budget). Spend is read from the Phase-24 analytics cost source
      # (`analytics_service.cost_summary`).
      budget_daily_usd: 5.0

      # require_approval: true (inherited from `default` or set here)
      # ALWAYS denies auto-dispatch for this project, regardless of
      # auto_dispatch/budget_daily_usd.
```

## Fail-closed guarantees

`autopilot_gate(project, pipeline, *, policies, budget)` in
`hivepilot/services/autopilot_queue.py` returns `GateDecision(allow=True, ...)`
**only if every one** of the following holds. Any missing, empty, or
malformed input denies — the gate never allows by default:

1. **Explicit allowlist.** `(project, pipeline)` must be present in that
   project's `auto_dispatch` list. A missing project entry, an empty list,
   or a pipeline name not in the list — all deny.
2. **No pending human gate.** The project's `require_approval` must be
   `False`. `True` (or an unresolved policy) denies.
3. **Positive, unexhausted budget.** `budget_daily_usd` must be set and
   `> 0`, and today's already-spent amount must be strictly less than that
   ceiling. No budget configured, a non-positive budget, an over-budget
   spend, or a budget check that raises — all deny.
4. **Never auto-merges.** The resolved pipeline's tasks are inspected via
   raw YAML (deliberately **not** `hivepilot/models.py`, to stay
   collision-free with parallel work on that file) for any `git.merge_pr:
   true`. If found — or if the pipeline/task can't be resolved at all —
   the gate denies. Autopilot can run a pipeline end-to-end, but it can
   never be the thing that merges a PR unattended.

An unknown pipeline, an unknown project, a missing budget hook result, or
any other unresolved input is treated as a "no" — never as "allow, since we
don't know better."

## The kill switch

```
hivepilot autopilot pause    # halts the drain within one tick
hivepilot autopilot resume   # clears pause AND stop
hivepilot autopilot stop     # halts the drain within one tick (stronger signal)
hivepilot autopilot status   # shows paused/stopped state + queue counts
```

`pause`/`stop` are checked at the very start of every drain tick
(`autopilot_queue.drain_one`) — a paused or stopped autopilot dispatches
nothing, full stop, regardless of what's queued or allowlisted.

## CLI reference

```
hivepilot autopilot enqueue <pipeline> <project> [--reason "..."] [--tenant default]
hivepilot autopilot queue    [--tenant default] [--state proposed]
hivepilot autopilot promote  <id>
hivepilot autopilot veto     <id>
hivepilot autopilot pause    [--tenant default]
hivepilot autopilot resume   [--tenant default]
hivepilot autopilot stop     [--tenant default]
hivepilot autopilot status   [--tenant default]
```

`enqueue` only ever accepts plain strings (pipeline name, project name,
free-text reason) — it never accepts or echoes a `RunResult`/step `detail`
payload, so a groomer pipeline's shell step can safely call it without any
risk of leaking run output into the queue's `reason` column.

## Example groomer pipeline (config only)

This is a completely ordinary HivePilot task/pipeline — nothing here is
special-cased by the engine beyond the final step being a shell command
that happens to call `hivepilot autopilot enqueue`.

`tasks.yaml`:

```yaml
tasks:
  groomer-scan:
    description: >
      Looks for stale docs / flaky tests / drift and proposes objectives
      into the Autopilot queue. Proposes only — never dispatches anything
      itself.
    steps:
      - name: scan for stale docs
        runner: claude
        prompt_file: prompts/agents/groomer.md
      - name: propose the finding
        runner: shell
        command: >
          hivepilot autopilot enqueue docs-refresh acme-api
          --reason "groomer-scan found 3 stale pages"
```

`pipelines.yaml`:

```yaml
pipelines:
  groomer-pipeline:
    stages:
      - name: groom
        task: groomer-scan
```

`schedules.yaml` — the fixed-cadence trigger for the groomer itself, plus
the dynamic drain schedule that consumes whatever it proposes:

```yaml
schedules:
  groomer-daily:
    task: groomer-scan
    projects: ["acme-api"]
    interval_minutes: 1440
    enabled: true

  autopilot-drain:
    source: autopilot     # dynamic entry -- mutually exclusive with `task`
    projects: []           # unused by the autopilot branch; kept for schema symmetry
    interval_minutes: 15
    enabled: true
```

`policies.yaml` — the allowlist that actually lets `docs-refresh` (a
*different*, presumably lower-risk pipeline than `groomer-pipeline` itself)
auto-dispatch once promoted:

```yaml
policies:
  projects:
    acme-api:
      auto_dispatch: ["docs-refresh"]
      budget_daily_usd: 2.0
```

With this configuration: `groomer-pipeline` runs daily and proposes
objectives; a human (or, once trusted, an automated promotion policy not
covered by this feature) calls `hivepilot autopilot promote <id>`; the
`autopilot-drain` schedule ticks every 15 minutes and dispatches at most one
allowlisted, budgeted, non-merging objective per tick.
