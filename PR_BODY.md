## Summary

<<<<<<< HEAD
HP-66: Agent Studio UI on the store-backed `/v1/roles` CRUD (HP-25).

- New System tab **Studio**: roster table + drawer editor.
- Reads for any token; New / Save / Delete only when `can('admin')`.
- Payload matches `RoleWrite` (name, title, profile, runner, model, inputs/outputs, can_block, order, prompt_text / prompt_file).
- Known roles reuse HP-20 `RoleAvatar`.

Does not add NL authoring (HP-24 Phase 3). Does not change the roles store or governance (`bypassPermissions` still fail-closed server-side).

Linear: [HP-66](https://linear.app/js-workspace/issue/HP-66/ui-agent-studio-front-crud-des-roles-backend-hp-25-livre). Parent HP-38.

## Testing

- [x] `cd web && npm test -- --run src/components/views/AgentStudioView.test.tsx src/components/Pollen.test.tsx` (29 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-BBCGRB4x.js`)

Replay: Pollen → Studio; admin token can create/edit/delete; read token sees the roster only.
=======
HP-50: nudge engine + structured verdict (`decision` + « je bloque si » + `file:line` findings).

An observer re-injects three signals to the owning role — without changing any gate:

- blocking CI / deterministic checks (`git_service.perform_git_actions`)
- blocking in-pipeline review (`Orchestrator._run_review`)
- file-ownership conflicts (`hivepilot ownership check --role`)

Each nudge persists `verdicts.kind="nudge"` (new `findings_json` / `block_if_json` columns) and posts a system message into the project's Orchestrateur Espace (HP-49) with an HP-47 action trace. Fail-safe: a broken nudge never raises into git/orchestrator.

Does not ingest GitHub review webhooks. Does not enforce ownership as a merge gate. Does not change `orchestrator.Verdict`. Disposition still unset. Banks stay `{project}:{task}:{role}` vs `role:{name}`.

Rebased onto current `main` (HP-55 / HP-53 / HP-20).

Linear: [HP-50](https://linear.app/js-workspace/issue/HP-50/nudge-engine-sortie-verdict-structuree-re-route-cireviewechec-je). Parent HP-31.

## Testing

- [x] `pytest tests/test_structured_verdict.py tests/test_nudge_engine.py tests/test_file_ownership.py tests/test_delegation.py tests/test_mission_plan.py tests/test_state_service.py -k verdict -q`
- [x] `ruff check` on new modules

Replay: trigger a failing check or `hivepilot ownership check --role developer`; open the project's Orchestrateur Espace — a `Nudge · …` system message with `je bloque si` and file:line findings.
>>>>>>> origin/main
