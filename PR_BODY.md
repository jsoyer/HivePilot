## Summary

HP-59: Composio and Pipedream Connect become **catalog sources** in the typed-tool table. Tools are listed, never executed. Missing vendor credentials refuse instead of inventing a connect.

- `GET /v1/tools/managed` — `{configured: bool}` only (no keys)
- `POST /v1/tools/composio/sync` `{toolkit}` — `GET https://backend.composio.dev/api/v3.1/tools`
- `POST /v1/tools/pipedream/sync` `{app}` — OAuth client-credentials then Connect components
- Hosts are hardcoded; foreign hosts are refused
- Settings: `HIVEPILOT_COMPOSIO_API_KEY`, `HIVEPILOT_PIPEDREAM_CLIENT_ID` / `_SECRET` / `_PROJECT_ID`
- Pollen Integrations cards replace the Coming-soon placeholders

Linear: [HP-59](https://linear.app/js-workspace/issue/HP-59/catalogues-manages-composio-pipedream-connect).

Replay: set the Composio key, open Pollen → System → Integrations, sync toolkit `github`. Confirm `composio__github__…` in Typed tools.

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp59_managed_catalogs.py tests/test_settings_secret_repr.py -q`
- [x] `cd web && npm test -- --run src/components/views/IntegrationsView.test.tsx src/lib/pollen-api.test.ts`
- [x] `python scripts/export_openapi.py && cd web && npm run generate:api`
