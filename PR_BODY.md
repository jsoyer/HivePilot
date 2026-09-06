## Summary

HP-60: Pollen **Integrations** page under System — one self-service hub for tool sources. The MCP command center tab (HP-76) stays as-is.

- OpenAPI paste → `POST /v1/tools/openapi/import`
- MCP HTTPS sync for registry HTTP servers → `POST /v1/mcp/servers/{id}/sync`
- Typed-tool catalog table → `GET /v1/tools`
- Plugin packs (HP-77) with admin install consent
- Composio + Pipedream: honest **Coming soon** cards (HP-59)

Writes gate on `useRole().can('admin')`.

Linear: [HP-60](https://linear.app/js-workspace/issue/HP-60/page-integrations-dans-pollen).

Replay: open Pollen → System → Integrations. Import a tiny OpenAPI doc; confirm the qualified name appears in Typed tools.

## Testing

- [x] `cd web && npm test -- --run src/components/views/IntegrationsView.test.tsx src/components/Pollen.test.tsx src/components/nav/nav-config.test.ts` (37 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-DRtTv1Wg.js`)
