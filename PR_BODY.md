## Summary

HP-64: typed Pollen client from FastAPI OpenAPI, plus a CI drift gate.

- Curated contract (`roles`, `concierge`, `schedules` + named trigger) exported to `web/openapi.json`.
- Generated TS types (`web/src/lib/generated/openapi.d.ts`) via `openapi-typescript`.
- CI job `OpenAPI client drift` fails if the spec or generated types are stale.
- Response models on roles / concierge / trigger so the spec is not `{}`.
- New `GET /v1/schedules`; Autopilot lists named schedules and can fire `POST /v1/webhook/trigger/{name}`.
- `fetchRole` + complete `RoleWritePayload` optional fields.

Linear: [HP-64](https://linear.app/js-workspace/issue/HP-64/client-ts-type-genere-depuis-lopenapi-fastapi-gate-anti-drift-cable).

Replay: `python scripts/export_openapi.py && cd web && npm run generate:api`.

## Testing

- [x] `pytest tests/test_openapi_contract.py tests/test_roles_api.py tests/test_concierge_endpoint.py`
- [x] `cd web && npm test -- --run src/lib/generated/contract.test.ts src/lib/pollen-api.test.ts src/components/views/SchedulesCard.test.tsx src/components/views/AutopilotView.test.tsx src/lib/i18n/fr.test.ts`
- [x] `cd web && npm run build` (Node 26.5.0 → `index-BVOaHhYS.js`)
- [x] `python scripts/export_openapi.py --check`
- [x] `ruff check` on touched Python
- [x] `mypy` on `api_service.py` / `openapi_contract.py` (CI typecheck fix: return models, not dicts)
