## Summary

HP-117: tenant-scoped logical package-tree taxonomy. Skills are classified by a `logical_id → category_path` mapping. Reclassify updates that mapping only — directory skills stay on disk. Ambiguous classifier output is `needs_review` (HP-97 PASS `taxonomy_review`), never a silent assign. No OpenSpace cloud, no disk-layout helper.

Owning issue: [HP-117](https://linear.app/js-workspace/issue/HP-117/u-23-taxonomie-package-tree-locale-no-cloud)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-98 skill catalog / origins / revisions. Patterns only from the OpenSpace package tree (`local_category_path`) — rewritten, not vendored.

Replay: `pytest tests/test_skill_taxonomy.py tests/test_skill_catalog.py tests/test_skill_trust.py`

## What changed

1. **`hivepilot/skill_taxonomy.py`** — `assign` / `reclassify` / `place` / `tree`. Mapping is tenant-scoped. Classifier: one clear path assigns; multiple / low-confidence / empty / flagged → `needs_review`.
2. **`state_service.init_db`** — `skill_taxonomy_placements` (`PRIMARY KEY (tenant, logical_id)`).
3. **Docs** — SKILLS / ARCHITECTURE / SECURITY record the local tree; cloud and disk materialize stay out of scope.

## Out of scope

- Disk moves / any function that materializes a category tree on disk
- Cloud taxonomy sync (browse / auth / upload / import)
- WhatsApp, HP-67, autonomous evolve

## Testing

- [x] `pytest tests/test_skill_taxonomy.py tests/test_skill_catalog.py tests/test_skill_trust.py tests/test_pass_store.py` — 62 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/skill_taxonomy.py` — no issues
