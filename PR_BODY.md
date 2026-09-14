## Summary

HP-114: optional BM25 + cosine hybrid via reciprocal rank fusion. Default path stays HP-107 BM25 (0 embed calls, 0 network). When a provider is passed or `HIVEPILOT_SKILL_EMBEDDINGS` is on **and** a provider is registered, ranks are fused with RRF (k=60). Document vectors cache in SQLite as JSON floats, keyed by `(revision_hash, model, dims)`. **Never pickle.** Cosine does not replace BM25.

Owning issue: [HP-114](https://linear.app/js-workspace/issue/HP-114/u-20-embeddings-hybrid-rrf-optionnel-no-pickle)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-107 BM25, HP-98 revision hashes, HP-112 `discover`.

Replay: `pytest tests/test_skill_embeddings.py tests/test_skill_ranker.py tests/test_host_skills.py`

## What changed

1. **`hivepilot/skill_embeddings.py`** — `EmbeddingProvider` protocol, RRF, cosine, JSON cache. Flag off ignores a registered provider.
2. **`hivepilot/skill_ranker.py`** — optional `provider` on `SkillRanker` / `retrieve`. Off ⇒ identical BM25 hits and scores.
3. **`hivepilot/host_skills.py`** — `discover` uses `configured_embedding_provider()` so hybrid is opt-in on the host path.
4. **`skill_embeddings` table** in `state_service.init_db()`.
5. **Docs** — SKILLS / SECURITY / ARCHITECTURE.

## Out of scope

- Shipping a mandatory or network embedding backend
- WhatsApp, HP-67 sandbox, cloud OpenSpace, autonomous evolve
- Vendored OpenSpace pickle / replace-only ranking

## Testing

- [x] `pytest tests/test_skill_embeddings.py tests/test_skill_ranker.py tests/test_host_skills.py` — 46 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/skill_embeddings.py hivepilot/skill_ranker.py hivepilot/host_skills.py` — no issues
- [x] CI mypy follow-up: typed `_skill(..., front=...)` and dropped `list.append(...) or` in host-skills tests (pre-existing on main, blocked `mypy hivepilot tests`)
- [x] Local `mypy hivepilot tests` — no issues (839 files)
- [x] CI pytest follow-up: `host_skills_enabled` + installer classification (HP-112 plugin gating) — 113 passed
