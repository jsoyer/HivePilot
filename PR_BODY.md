## Summary

HP-107: local Okapi BM25 skill retrieval. OpenSpace `skill_ranker` BM25 stage rewritten — **no** vendor BM25 package, **0 model queries**, no pickle, no cloud. Order is deterministic. HP-105 enabled / provisional filters run **before** scoring. Progressive disclosure: name+description for ranking, `SKILL.md` body only after `disclose`.

Owning issue: [HP-107](https://linear.app/js-workspace/issue/HP-107/u-13-bm25-retrieval-local-deterministe)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-98 catalog and HP-105 trust.

Replay: `pytest tests/test_skill_ranker.py tests/test_skill_catalog.py tests/test_skill_trust.py`

## What changed

1. **`hivepilot/skill_ranker.py`** — pure-Python BM25, catalog+trust prefilter, `retrieve` / `disclose`.
2. **`tests/test_skill_ranker.py`** — French golden ranking, filter-before-IDF, progressive disclosure, stable order.
3. **Docs** — SKILLS / SECURITY / ARCHITECTURE note local BM25 and body-after-selection.

## Out of scope

- HP-114 hybrid embeddings / RRF, HP-112 host skills, HP-108 skill→tools, HP-109 apply
- WhatsApp, HP-67 sandbox, vendored OpenSpace / pickle / cloud

## Testing

- [x] `pytest tests/test_skill_ranker.py tests/test_skill_catalog.py tests/test_skill_trust.py` — 45 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/skill_ranker.py` — no issues
