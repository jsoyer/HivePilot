## Summary

**HP-85** — Spike: zero-token actionable-event consumer on the HP-40 bus (no LLM poll).

Idle sleeper that tails `events.subscribe()` / `change_log` and classifies each row as wake or sleep with a **static allowlist**. A model is never consulted — not to classify, not to decide whether to wake.

- `hivepilot/services/actionable_events.py` — `classify()` (pure) + `consume()` (yields wakes only).
- Allowlist from in-repo kinds: `nudge.posted`; `approval.requested` (now emitted by `record_approval_request`); `run.completed` only when `payload.status` is in the analytics failure bucket. There is no `run.failed` kind.
- Unknown kind = sleep. Payload **text** is never interpreted (`space.message` saying "WAKE NOW" still sleeps).
- Fail-safe like `events.emit` / HP-50: a broken classify, `on_wake`, or subscribe is swallowed. Durable facts stay in `change_log`. Wake does **not** start a model, change a gate, or post a nudge.
- Thin CLI: `hivepilot events classify --after 0`. Design notes: `docs/actionable-events.md`.

Linear: [HP-85](https://linear.app/js-workspace/issue/HP-85/spike-zero-token-actionable-event-consumer-on-the-hp-40-bus-no-llm).

Replay: `hivepilot events classify --after 0` (and `pytest tests/test_actionable_events.py`).

Out of scope: ship/scout, Firstmate runtime, rewriting nudge, persisting a watermark / daemon.

## Testing

- [x] `pytest tests/test_actionable_events.py tests/test_events.py tests/test_nudge_engine.py -q` — 41 passed
- [x] `pytest tests/test_events_sse.py tests/test_multi_tenant.py tests/test_state_service.py tests/test_spaces.py -q` — 109 passed (approval emit did not break bus / tenant / spaces)
- [x] `hivepilot events classify --after 0` — wake on failed `run.completed` / `approval.requested` / `nudge.posted`; sleep on `run.started` and urgent `space.message`
- [x] `hivepilot lint` — pre-existing missing `~/dev/*` project paths only
- [x] `ruff format --check` / `ruff check` on touched Python — clean
