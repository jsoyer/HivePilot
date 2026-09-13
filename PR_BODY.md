## Summary

HP-100: unique `idempotency_key` on table `side_effects`, plus a `pending_tool` checkpoint around PASS/approval. Resume reuses the same key. Volatile catalog tools skip the effect cache. Crash mid-approval executes the effect exactly once.

Owning issue: [HP-100](https://linear.app/js-workspace/issue/HP-100/u-06-idempotency-checkpoint-resume)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-97 PASS + HP-95 tool catalog.

Replay: `pytest tests/test_side_effects.py tests/test_checkpoints.py tests/test_pass_store.py tests/test_tool_catalog.py`

## What changed

1. **`hivepilot/side_effects.py`** — unique `idempotency_key`; persist `side_effects`; volatile rows complete without caching a payload.
2. **`hivepilot/checkpoints.py`** — `kind=pending_tool` around PASS `submit`/`decide`; `resume` reuses the reserved key and CAS-claims so the effect runs at most once.
3. **State store** — `side_effects` + `checkpoints` tables in `init_db` (same CREATE IF NOT EXISTS pattern as HP-97 / HP-99).

## Out of scope

- HP-101 memory proposals HITL
- HP-102 presenter parity, HP-103 skill doctrine, HP-105 trust
- WhatsApp, HP-67 sandbox, vendored TS / Electron / pickle

## Testing

- [x] `pytest tests/test_side_effects.py tests/test_checkpoints.py tests/test_pass_store.py tests/test_tool_catalog.py` — 54 passed
- [x] `ruff check` + `ruff format --check` clean
