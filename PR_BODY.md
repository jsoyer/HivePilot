## Summary

HP-105: local skill trust ladder. New revisions are **provisional**; `enabled` is orthogonal. Unknown revisions are not implicitly trusted or enabled. Promotion after N distinct successful inter-runs; attributed failure demotes; ambiguous failure opens a PASS review (no auto-demote).

Owning issue: [HP-105](https://linear.app/js-workspace/issue/HP-105/u-11-trust-provisionaltrusted-enabled-orthogonal)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-104 skill events, HP-98 catalog, HP-97 PASS.

Replay: `pytest tests/test_skill_trust.py tests/test_skill_events.py tests/test_skill_catalog.py tests/test_pass_store.py`

## What changed

1. **`hivepilot/skill_trust.py`** — `register_revision` (provisional + enabled), `set_enabled`, `evaluate_promotion` (distinct HP-104 `completed` runs; default N=2 via `HIVEPILOT_SKILL_TRUST_PROMOTION`), `report_failure` (attributed → demote; ambiguous → PASS `kind=skill_evolution` / `action=trust_review`; `not_skill` ignored). HP-106 attribution is a closed-vocab stub.
2. **State store** — `skill_trust_states` + `skill_trust_observations` in `init_db`.
3. **Docs** — SKILLS / SECURITY / ARCHITECTURE note the ladder. Catalog and events stay free of OpenSpace cloud / pickle.

## Out of scope

- HP-106 full signals/attribution, HP-107 BM25, HP-108 skill→tools, HP-109 apply
- WhatsApp, HP-67 sandbox, vendored OpenSpace / pickle / cloud

## Testing

- [ ] `pytest tests/test_skill_trust.py`
- [ ] `pytest tests/test_skill_events.py tests/test_skill_catalog.py tests/test_pass_store.py`
- [ ] `ruff check` + `ruff format --check` on touched Python
