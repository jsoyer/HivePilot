## Summary

<<<<<<< HEAD
HP-22: Pollen Chat tab that talks to the existing concierge brain (`POST /v1/concierge` → `concierge_service.route()`).

- New Operate tab **Chat** (Grok-style bubbles + composer).
- `route` / `action` / `multi_route` render as proposal cards with HP-20 `RoleAvatar`. Classify-only: does not dispatch runs.
- Same fail-closed semantics as Telegram. Independent of HP-18 (OSS runner).

Rebased onto current `main` (HP-20 avatars + HP-55/53 + Espaces/Orchestrator/MCP). Additive merge: Chat leads Operate, Spaces and Orchestrator stay.

Does not execute proposals. Does not gate on `chatops_concierge_enabled`. Does not touch HP-18.

Linear: [HP-22](https://linear.app/js-workspace/issue/HP-22/pollen-interactive-agent-chat-grok-bot-style-via-the-concierge).

## Testing

- [x] `cd web && npm test -- --run src/components/views/ChatView.test.tsx src/components/Pollen.test.tsx` (28 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-ChuVuSe5.js`)
- [x] `pytest tests/test_concierge_endpoint.py`

Replay: Pollen → Chat; send a message; answers render as bubbles, routes as proposal cards.
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
