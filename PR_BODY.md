## Summary

HP-50: nudge engine + structured verdict (`decision` + « je bloque si » + `file:line` findings).

An observer re-injects three signals to the owning role — without changing any gate:

- blocking CI / deterministic checks (`git_service.perform_git_actions`)
- blocking in-pipeline review (`Orchestrator._run_review`)
- file-ownership conflicts (`hivepilot ownership check --role`)

Each nudge persists `verdicts.kind="nudge"` (new `findings_json` / `block_if_json` columns) and posts a system message into the project's Orchestrateur Espace (HP-49) with an HP-47 action trace. Fail-safe: a broken nudge never raises into git/orchestrator.

Does not ingest GitHub review webhooks. Does not enforce ownership as a merge gate. Does not change `orchestrator.Verdict`. Disposition still unset. Banks stay `{project}:{task}:{role}` vs `role:{name}`.

Linear: [HP-50](https://linear.app/js-workspace/issue/HP-50/nudge-engine-sortie-verdict-structuree-re-route-cireviewechec-je). Parent HP-31.

## Testing

- [x] `pytest tests/test_structured_verdict.py tests/test_nudge_engine.py tests/test_file_ownership.py tests/test_delegation.py tests/test_mission_plan.py tests/test_state_service.py -k verdict -q`
- [x] `ruff check` on new modules

Replay: trigger a failing check or `hivepilot ownership check --role developer`; open the project's Orchestrateur Espace — a `Nudge · …` system message with `je bloque si` and file:line findings.
