## Summary

HP-99: tenant-scoped evidence refs, bounded packets, and ingest redaction. Evolution claims must cite existing refs in the same tenant; missing refs make a `skill_evolution` proposal not admissible. Secrets are redacted at ingest. Watermark is `change_log.id`.

Owning issue: [HP-99](https://linear.app/js-workspace/issue/HP-99/u-05-evidence-refs-paquets-bornes-redaction)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-97 PASS + HP-98 skill catalog.

Replay: `pytest tests/test_evidence.py tests/test_pass_store.py tests/test_skill_catalog.py`

## What changed

1. **`hivepilot/evidence.py`** — tenant-scoped ref registry; ingest redacts registered secrets and secret-looking metadata keys; each ingest emits HP-40 `change_log` and stores **watermark = `change_log.id`**.
2. **Bounded packets** — `build_packet` caps chars/refs, lists omitted and missing ids (no silent drop).
3. **Admissibility** — `assess_evolution_claim` is fail-closed: empty or missing/foreign-tenant refs ⇒ not admissible.
4. **PASS gate** — `submit(kind=skill_evolution)` persists PENDING first (HP-97), then rejects when the claim is not admissible. `create_pending` is unchanged.

## Out of scope

- HP-109 FIX/DERIVED/CAPTURED apply paths
- HP-104 events panel, HP-105 trust, HP-100/101
- WhatsApp, HP-67 sandbox, OpenSpace cloud / pickle / vendored TS

## Testing

- [ ] `pytest tests/test_evidence.py tests/test_pass_store.py tests/test_skill_catalog.py`
- [ ] `ruff check` + `ruff format --check` clean
