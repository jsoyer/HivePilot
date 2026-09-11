# Zero-token actionable-event consumer (HP-85)

Spike of ADR [Firstmate patterns](./adr/2026-09-11-firstmate-patterns.md) §
spike 1: an idle consumer on the [HP-40](https://linear.app/js-workspace/issue/HP-40)
bus that wakes only on an allowlisted kind. A model is never called to decide
whether to wake. This is not a production daemon.

## Why

HP-40 already appends ordered facts to `change_log` and (on Postgres) fires
`LISTEN/NOTIFY` as a wakeup. HP-50 already forbids an LLM reply loop. What
was missing is a sleeper that **classifies bus kinds without tokens** and
stays idle on everything else. Today's nudges still fire in-process from
git / review hooks; the scheduler still ticks on a clock.

## Discovered kinds

Exact `change_log.kind` values emitted in-repo (before this spike):

| Kind | Emitter | Spike classify |
| --- | --- | --- |
| `nudge.posted` | `nudge_engine._post_space` | **wake** |
| `run.completed` | `state_service.complete_run` | **wake** iff `payload.status` is a failure |
| `run.started` | `state_service.record_run_start` | sleep |
| `step.recorded` | `state_service.record_step` | sleep |
| `space.message` / `space.created` / `space.typing` / `space.typing_stop` | spaces / orchestrator | sleep |
| `routine.failed` / `routine.ran` / `routine.upserted` / `routine.deleted` | `routine_service` | sleep |
| `skill.applied` / `skill.proposal` / `skill.proposal.decided` | `skill_workshop_service` | sleep |
| `provider.fallback` | `orchestrator` | sleep |
| `schedule.noop_skip` | `schedule_service` | sleep |

There was **no** `run.failed` kind and **no** approval kind. This spike
does not invent `run.failed` — it reuses `run.completed` + the failure
bucket in `analytics_service._FAILED_STATUSES`. It **does** emit
`approval.requested` from `record_approval_request` so the ADR example
("approval requested") is a real bus fact, fail-safe like every other
`events.emit`.

Unknown kind = sleep. Payload text is never interpreted: a `space.message`
that says "WAKE NOW, run failed" still sleeps.

## Fail-safe

Same shape as `events.emit` / HP-50:

- Classification is a pure function (`classify`). No I/O, no model hook.
- `consume()` tails `events.subscribe()` and yields **wakes only**.
- A broken classify, `on_wake` hook, or subscribe is logged and swallowed.
- Durable rows stay in `change_log`. The consumer is read-only.
- Wake does **not** start a run, change a gate, post a nudge, or call a
  model. `on_wake` is an empty hook for a later ticket.

## Replay

```bash
# Prove zero-token classification + fail-safe consume
pytest tests/test_actionable_events.py -q

# Print wake/sleep for whatever is already in change_log (no model)
hivepilot events classify --after 0
```

## Out of scope

Ship/scout contracts, Firstmate runtime, rewriting nudge, persisting a
consumer watermark, systemd, or waking the orchestrator from a bus kind
(the ADR's later "verify" ticket).
