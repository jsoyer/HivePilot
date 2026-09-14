## Summary

HP-109: draft-only FIX / DERIVED / CAPTURED skill-evolution proposals into the HP-97 PASS inbox. No skill file or `.skill_id` write. CAPTURED requires independent validation (execution ref + a distinct validation ref; same-run `completed` is not enough). Merge keys are idempotent. OpenSpace autonomous evolution mode is a hard no-go. Atomic accept stays HP-111.

Owning issue: [HP-109](https://linear.app/js-workspace/issue/HP-109/u-15-propositions-fixderivedcaptured-draft-only)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-97 PASS, HP-98 origins, HP-99 evidence, HP-106 FIX triple / `draft_fix_proposal`.

Replay: `pytest tests/test_skill_evolution.py tests/test_skill_signals.py tests/test_pass_store.py tests/test_evidence.py`

## What changed

1. **`hivepilot/skill_evolution.py`** — `propose` / `propose_fix` persist PENDING `kind=skill_evolution` via `create_pending` (never `submit` / `match_auto`). CAPTURED gate + merge-key idempotency. `apply_approved` / `preview_accept` are HP-111 hooks that refuse mutation.
2. **`hivepilot/skill_signals.py`** — `draft_fix_proposal` stays the eligibility stub (`persisted=False`); persist is `propose_fix`.
3. **Docs** — SKILLS / SECURITY / ARCHITECTURE note draft-only, CAPTURED independence, no autonomous apply.

## Out of scope

- HP-110 deterministic validator, HP-111 atomic accept / Pollen diff UI (hooks only)
- HP-112, HP-114, WhatsApp, HP-67 sandbox
- Vendored OpenSpace / pickle / cloud / `EVOLUTION_MODE=autonomous`

## Testing

- [x] `pytest tests/test_skill_evolution.py tests/test_skill_signals.py tests/test_pass_store.py tests/test_evidence.py tests/test_skill_catalog.py tests/test_presenters.py` — 97 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
