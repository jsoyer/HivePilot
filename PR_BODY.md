## Summary

**HP-27** — Agent Studio Phase 3: natural-language agent authoring.

The headline of Agent Studio: describe an agent in plain language → HivePilot generates the full config → you review/tweak in the builder → save.

- `POST /v1/roles/draft` (admin-gated) turns a free-text spec into a **RoleWrite proposal** (runner, model/profile, prompt, `can_block`, inputs/outputs). Nothing is written to the store.
- Fail-closed no-tools LLM path: same concierge/OSS model and `--tools ""` invariant as HP-18/HP-22. `allowed_tools` / `bypassPermissions` in model JSON are stripped. A human admin saves via existing CRUD; `lint_role_draft` validates the skeleton.
- Pollen Agent Studio: **Describe your agent** box → pre-fills the create form → Save still calls `POST /v1/roles`.

Linear: [HP-27](https://linear.app/js-workspace/issue/HP-27/agent-studio-phase-3-natural-language-agent-authoring).

Replay: `hivepilot run example-api docs` (draft path is unit-tested with a mocked LLM; no live model required).

## Testing

- [x] `pytest tests/test_role_draft_service.py tests/test_roles_draft_api.py tests/test_roles_api.py tests/test_openapi_contract.py -q` — 45 passed
- [x] `npm test --prefix web -- --run src/components/views/AgentStudioView.test.tsx src/lib/pollen-api.test.ts src/lib/generated/contract.test.ts src/lib/i18n/en.test.ts src/lib/i18n/fr.test.ts` — 86 passed
- [x] `ruff check` / `ruff format --check` on touched Python — clean
- [x] `hivepilot lint` — pre-existing missing `~/dev/*` project paths only
