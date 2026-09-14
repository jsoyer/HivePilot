## Summary

HP-113: 0-model eval contract in default PR CI, plus an opt-in nightly behavior-memory suite. Denied / traversal / pending never mutate; approve×2 is one HP-100 side-effect. The harness takes explicit `gate` × `surface` × `token`/`path` — no keyword routing.

Owning issue: [HP-113](https://linear.app/js-workspace/issue/HP-113/u-19-eval-contract-ci-behavior-memory-opt-in)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-95 catalog, HP-97 PASS, HP-100 idempotency, HP-101 memory HITL.

Replay: `pytest tests/test_eval_contract.py tests/test_eval_behavior_memory.py`
Nightly (opt-in): `HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1 pytest -m behavior_memory`

## What changed

1. **`hivepilot/eval_contract.py`** — deterministic contract harness (no LLM). `check_suite()` asserts denied/traversal/pending → 0 effects and approve×2 → 1 effect via HP-100 keys.
2. **`hivepilot/eval_behavior_memory.py`** — HITL memory scenarios, gated on `HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1`.
3. **CI** — default `pytest` excludes `@pytest.mark.behavior_memory`. `.github/workflows/nightly.yml` runs that marker when dispatched or when repository variable `HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1`.
4. **Docs** — SECURITY / ARCHITECTURE record the contract and the opt-in nightly flag.

## Out of scope

- HP-114 hybrid RRF
- WhatsApp, HP-67 sandbox, cloud OpenSpace, autonomous evolve
- Vendored Coworker evals / keyword skill routing

## Testing

- [x] `pytest tests/test_eval_contract.py tests/test_eval_behavior_memory.py tests/test_checkpoints.py tests/test_memory_proposals.py tests/test_skill_evolution_validator.py tests/test_side_effects.py tests/test_pass_store.py` — 93 passed (1 brittle docstring assert fixed)
- [x] `HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1 pytest tests/test_eval_contract.py tests/test_eval_behavior_memory.py tests/test_eval_behavior_memory_nightly.py` — 16 passed
- [x] without the flag, nightly is skipped; `pytest -m "not behavior_memory"` deselects it (15 passed / 1 deselected)
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/eval_contract.py hivepilot/eval_behavior_memory.py` — no issues
