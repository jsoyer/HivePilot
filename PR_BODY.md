## Summary

HP-104: skill-cycle event store + Pollen top/bottom panel. Six event types, idempotent per `(revision, run, step, type)`. Absence of a measurement is not zero.

Owning issue: [HP-104](https://linear.app/js-workspace/issue/HP-104/u-10-events-skill-cycle-panneau-pollen)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-98 skill catalog + HP-99 evidence.

Replay: `pytest tests/test_skill_events.py tests/test_skill_events_panel.py tests/test_skill_orchestrator_wiring.py tests/test_skill_catalog.py tests/test_actionable_events.py`

## What changed

1. **`hivepilot/skill_events.py`** — `record_skill_event` / `record_cycle` with types `selected`, `invoked`, `applied`, `completed`, `fallback`, `excluded`. Unique key `(revision_id, run_id, step, event_type)`. `rank_skills()` omits unmeasured skills (`rate is None` when `selected == 0`).
2. **State store** — `skill_cycle_events` table in `init_db`.
3. **Orchestrator** — fail-safe cycle writes at resolve / apply / fallback / step success. HP-79 `skill_usage_events` unchanged.
4. **Pollen panel** — opt-in `skill-cycle` (`HIVEPILOT_SKILL_EVENTS_PANEL_ENABLED`) shows top/bottom measured skills.

## Out of scope

- HP-105 trust ladder, HP-106 signals, HP-108 skill→tools
- WhatsApp, HP-67 sandbox, vendored OpenSpace / pickle / cloud

## Testing

- [x] `pytest tests/test_skill_events.py tests/test_skill_events_panel.py` — 21 passed
- [x] `pytest tests/test_skill_orchestrator_wiring.py tests/test_actionable_events.py tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag` — 104 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
