## Summary

HP-110: deterministic skill-evolution validator + safety load. Proposed skill paths/content are inspected with structural checks (not regex alone). Result ∈ {approve, reject, needs_human_review}. Zero mutation. Privilege widening (allowed-tools / hooks / shell / permissions) is refused unless a **specific** approval names the grant.

Owning issue: [HP-110](https://linear.app/js-workspace/issue/HP-110/u-16-validator-deterministe-safety-load)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-109 drafts, HP-99 evidence/redaction, HP-98 directory load safety.

Replay: `pytest tests/test_skill_evolution_validator.py tests/test_skill_evolution.py`

## What changed

1. **`hivepilot/skill_evolution_validator.py`** — `validate` / `validate_proposal` inspect files and optional `skill_root`. Rejects traversal/symlink, size, UTF-8, pickle, frontmatter, secrets. Privilege extensions without a named approval → `needs_human_review`. Never writes.
2. **`hivepilot/skill_evolution.py`** — `propose` gates on reject (no PASS row). `preview_accept` includes the HP-111 validation hook (`would_mutate` stays false).
3. **Docs** — SKILLS / SECURITY / ARCHITECTURE record the read-only gate.

## Out of scope

- HP-111 atomic accept / Pollen diff UI (`validate()` hook only)
- HP-112, HP-114, WhatsApp, HP-67 sandbox
- Vendored OpenSpace / pickle / cloud / autonomous apply

## Testing

- [x] `pytest tests/test_skill_evolution_validator.py tests/test_skill_evolution.py tests/test_pass_store.py tests/test_evidence.py` — 76 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/skill_evolution_validator.py hivepilot/skill_evolution.py` — no issues
