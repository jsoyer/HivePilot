## Summary

HP-112: local host skills `skill-discovery` + `delegate-task`. Discovery is HP-107 BM25 over the HP-98 catalog (HP-105 filters first, progressive disclosure). Delegate is HivePilot `run_subagent` / `spawn_peer` / `Orchestrator.run_pipeline` (or a registered pipeline runner). Capability specs grant no HP-95 tool tokens and do not attach a skill to a stage.

Owning issue: [HP-112](https://linear.app/js-workspace/issue/HP-112/u-18-host-skills-skill-discovery-delegate-task-local)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-107 BM25, HP-98 catalog, HP-108 skill→tools (no keyword force-load), HP-48 delegation.

Replay: `pytest tests/test_host_skills.py tests/test_skill_ranker.py tests/test_delegation.py`

## What changed

1. **`hivepilot/host_skills.py`** — `discover` / `disclose` / `delegate`. Host SkillSpecs for `skill-discovery` and `delegate-task`.
2. **`hivepilot/bundled_plugins/host_skills.py`** — publishes those specs when plugins load.
3. **Docs** — SKILLS / ARCHITECTURE / SECURITY record the local host surface.

## Out of scope

- WhatsApp, HP-67 sandbox, HP-114 hybrid RRF
- Autonomous skill evolution, vendored OpenSpace, remote skill host

## Testing

- [x] `pytest tests/test_host_skills.py tests/test_skill_ranker.py tests/test_delegation.py tests/test_skill_capabilities.py tests/test_skill_catalog.py` — 80 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/host_skills.py` — no issues
