## Summary

HP-98: read-only skill catalog + in-memory revision DAG. Scan directory skills (`skill_dirs`) and plugin `register()["skills"]` without writing `.skill_id` sidecars. Origins IMPORTED / FIXED / DERIVED / CAPTURED. One active revision per logical skill. Content-hash change → new revision, same logical id.

Owning issue: [HP-98](https://linear.app/js-workspace/issue/HP-98/u-04-skill-catalog-dag-revisions-scan-read-only)

ADR / plan: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) (patterns only, rewrite Python; no OpenSpace cloud SSOT; no vendored TS). SSOT plan: `coworker-openspace`.

Replay: `pytest tests/test_skill_catalog.py`

## What changed

1. **`hivepilot/skill_catalog.py`** — OpenSpace `skill_engine` types/store/patch **patterns**, rewritten. Deterministic logical ids from the skill name. Scan never writes disk.
2. **Tests** — no sidecar writes; origin enum; one active revision; content change keeps logical id (`FIXED`).
3. **Docs** — `docs/SKILLS.md` + a pointer in `docs/ARCHITECTURE.md`.

## Out of scope

- HP-105 trust provisional↔trusted
- HP-104 events / Pollen panel
- HP-99 evidence refs
- WhatsApp / HP-67 desktop reopen
- Workshop accept/reject path (HP-79 unchanged)

## Testing

- [x] `pytest tests/test_skill_catalog.py` — 13 passed
- [x] `pytest` skill-related suites (`test_skill_dirs`, `test_skills_registry`, `test_skill_workshop_service`, `test_skill_catalog`, `test_sample_skill`, `test_skill_config_validation`, `test_skill_application`, `test_skill_orchestrator_wiring`) — 101 passed
- [x] `ruff check` + `ruff format --check` on `hivepilot/skill_catalog.py` and `tests/test_skill_catalog.py`
