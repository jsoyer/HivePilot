## Summary

HP-106: local causal attribution for skill failures. Detector + linker (OpenSpace signals pattern, rewritten) distinguish **tool / env / permission / skill defect**. A network outage is `env` and must not demote HP-105 trust. FIX is gated on revision + causal event + representative result (apply stays HP-109).

Owning issue: [HP-106](https://linear.app/js-workspace/issue/HP-106/u-12-signaux-attribution-causale)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-104 skill events, HP-105 trust, HP-97 PASS.

Replay: `pytest tests/test_skill_signals.py tests/test_skill_trust.py tests/test_skill_events.py`

## What changed

1. **`hivepilot/skill_signals.py`** — classify failure class, link invoked/applied HP-104 events, map to HP-105 `attributed` / `ambiguous` / `not_skill`. `assess_fix_eligibility` requires the FIX triple; `draft_fix_proposal` is a non-persisting stub.
2. **`hivepilot/skill_trust.py`** — `report_failure` / `classify_attribution` call the real classifier (closed-vocab stub removed). `TrustDecision.failure_class` is populated.
3. **Docs** — SKILLS / SECURITY / ARCHITECTURE note the classes and the network-must-not-demote rule.

## Out of scope

- HP-109 FIX/DERIVED/CAPTURED apply, HP-107 BM25, HP-108 skill→tools
- WhatsApp, HP-67 sandbox, vendored OpenSpace / pickle / cloud

## Testing

- [x] `pytest tests/test_skill_signals.py tests/test_skill_trust.py tests/test_skill_events.py tests/test_skill_catalog.py tests/test_pass_store.py` — 85 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
