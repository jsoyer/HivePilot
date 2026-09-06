## Summary

HP-58: import an HTTPS MCP server or an OpenAPI/Swagger document into a **typed-tool catalog**. Credentials stay encrypted at rest, fetches go through an SSRF allowlist, and qualified names de-collide instead of overwriting.

- `POST /v1/mcp/servers/{id}/sync` — JSON-RPC `tools/list` (admin). Import still does not fetch (HP-76).
- `POST /v1/tools/openapi/import` — paste or allowlisted URL. Pasting OpenAPI into `/v1/mcp/import` is rejected and pointed here.
- `GET /v1/tools` — `{kind}__{source}__{local}` names (Claude `mcp__server__tool` shape). A second source that would clash gets `__2`.
- Literal header/env secrets require `HIVEPILOT_CREDENTIALS_KEY` (Fernet). GET responses expose `has_credentials` only. Without the key, keep `${env:NAME}` refs.
- SSRF: loopback http(s) allowed; remote HTTPS only; no userinfo; no redirects; private / link-local / metadata DNS (incl. IPv4-mapped) refused.
- MCP server names no longer silently overwrite (`github-2`). Catalog add of an already-installed name returns the existing row.

Does **not** rebuild `McpView` (HP-76). Does **not** invoke tools or inject registry servers into Claude `--mcp-config`.

Linear: [HP-58](https://linear.app/js-workspace/issue/HP-58/import-mcp-https-openapi-outils-types-creds-chiffres-garde-ssrf-de).

Replay:

```bash
# OpenAPI paste
hivepilot tokens add admin
curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"{\"openapi\":\"3.1.0\",\"info\":{\"title\":\"Demo\"},\"paths\":{\"/ping\":{\"get\":{\"operationId\":\"ping\"}}}}","name":"demo"}' \
  http://127.0.0.1:8765/v1/tools/openapi/import

# MCP HTTPS: import URL (stored, not fetched), then sync (admin)
curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"https://mcp.example.com/sse"}' \
  http://127.0.0.1:8765/v1/mcp/import
```

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 python -m pytest tests/test_hp58_typed_tools.py tests/test_mcp_registry.py tests/test_settings_secret_repr.py` (100 passed)
- [x] `python -m mypy hivepilot tests` (760 source files, no issues)
- [x] `ruff check` + `ruff format --check` on the HP-58 modules
- [x] `python scripts/export_openapi.py --check` then `cd web && npm run generate:api`
- [x] `cd web && npm test -- --run src/lib/pollen-api.test.ts src/components/views/McpView.test.tsx` (62 passed: 58 pollen-api + 4 McpView)
- [x] `cd web && npm run build` (Node 26.5.0; static unchanged — new helpers are unused by `McpView`)
