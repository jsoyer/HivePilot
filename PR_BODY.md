## Summary

HP-55: Pollen Memory → **Knowledge** tab — Mental Models + Observations on each Hindsight identity bank (`role:{name}`).

- `GET /v1/hindsight/status` — role picker + configured flag (no server call).
- `GET /v1/hindsight/roles/{role}` — models (`detail=content`) + observations (`type=observation`).
- Writes (`run`): create / PATCH mental model, refresh, PATCH source facts (observations are derived).
- Proof count + quotes + optional confidence. Disabled/missing client degrades (`configured: false`).
- Search/mem0 tab stays until the retirement slice.

Does not touch HP-51 episodic banks `{project}:{task}:{role}`. Disposition still unset.

Linear: [HP-55](https://linear.app/js-workspace/issue/HP-55/panneau-memoire-pollen-mental-models-observations-preuvesconfiance). Parent HP-33.

## Testing

- [x] `pytest tests/test_hindsight_panel.py tests/test_api_hindsight_panel.py tests/test_pollen_contract.py -q`
- [x] `cd web && npm test -- --run src/components/views/RoleMemoryView.test.tsx src/components/views/MemoryView.test.tsx src/lib/pollen-api.test.ts src/lib/i18n/fr.test.ts`
- [x] `cd web && npm run build` (Node 26.5.0)
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: `HIVEPILOT_HINDSIGHT_ENABLED=true`, open Pollen → Memory → Knowledge, pick a role, create a mental model, correct a source fact.
