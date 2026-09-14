## Summary

HP-111: atomic accept of drafted skill-evolution proposals after HITL, plus Pollen multi-file diff and `@xyflow` lineage. Builds on HP-109 drafts and HP-110 `validate()` / `validate_proposal()` (not reimplemented).

Owning issue: [HP-111](https://linear.app/js-workspace/issue/HP-111/u-17-acceptation-atomique-difflineage-pollen)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`.

Replay: `pytest tests/test_skill_evolution_accept.py tests/test_skill_evolution.py tests/test_skill_evolution_validator.py tests/test_api_skill_evolution.py`

## What changed

1. **`hivepilot/skill_evolution_accept.py`** — `apply_approved` writes directory files only after PASS `APPROVED`. Rejected / PENDING refuse. `validate_proposal()` runs first (reject blocks; `needs_human_review` stays HITL). Expected digest and drifted baseline are stale. Double accept is a no-op. Interrupted writes recover from a sibling `.hp111` journal + staging tree.
2. **`hivepilot/skill_evolution.py`** — `preview_accept` reports `would_mutate` only when APPROVED and not yet applied. `file_diffs` / `lineage_graph` feed Pollen. `propose` persists `base_files` / `base_digest`.
3. **API + Pollen workshop** — `GET/POST /v1/skill-evolutions…` (read list, approve-rank decide/accept). Workshop shows per-file diffs and an `@xyflow` DAG. Accept stays disabled until APPROVED.

## Out of scope

- WhatsApp, HP-67 sandbox, HP-112 / HP-114
- Vendored OpenSpace / pickle / cloud / autonomous apply

## Testing

- [ ] `pytest tests/test_skill_evolution_accept.py tests/test_skill_evolution.py tests/test_skill_evolution_validator.py tests/test_api_skill_evolution.py tests/test_pass_store.py`
- [ ] `ruff check` + `ruff format --check` on touched Python
- [ ] `mypy hivepilot/skill_evolution_accept.py hivepilot/skill_evolution.py`
- [ ] `npm test` in `web/` for workshop + lineage
