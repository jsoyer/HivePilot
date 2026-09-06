## Summary

HP-77: **declarative, shareable plugin packs** — a YAML manifest (curated plugins + config refs + credential *names* + host/capability guards) installable in one gesture. The hub is metadata only; plugin code still only comes from `plugins install`.

- Bundled packs: `skills-kit` (improve + shadcn), `obsidian-memory` (obsidian + hindsight).
- CLI: `hivepilot plugins packs list|info|install`.
- API: `GET /v1/plugin-packs`, `GET /v1/plugin-packs/{name}` (includes shareable YAML), `POST /v1/plugin-packs/import`, `POST /v1/plugin-packs/{name}/install` (`consent: true`), `GET /v1/plugin-packs-hub`.
- Fail-closed: unknown / demo / agent-CLI plugin names refused; `HIVEPILOT_*TOKEN`/`*KEY`/`*SECRET` literals refused (`${env:}` / `${secret:}` only).
- Preview reports platform/version blockers, capability-policy gaps, and missing credential env vars.
- Optional hub: `HIVEPILOT_PLUGIN_PACKS_INDEX_URL`.

Does **not** add a Pollen page (HP-60) and does **not** fetch plugin code from the hub.

Linear: [HP-77](https://linear.app/js-workspace/issue/HP-77/packs-de-plugins-declaratifs-and-partageables-hub-type-clawhub).

Replay:

```bash
hivepilot plugins packs list
hivepilot plugins packs info skills-kit
hivepilot plugins packs install skills-kit --yes
```

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 python -m pytest tests/test_hp77_plugin_packs.py tests/test_bundled_plugins_layout.py` (52 passed)
- [x] `python -m mypy hivepilot tests`
- [x] `ruff check` on the HP-77 modules
- [x] `python scripts/export_openapi.py --check` then `cd web && npm run generate:api` (committed `openapi.d.ts` after CI drift)
- [x] `cd web && npm test -- --run src/lib/pollen-api.test.ts` (60 passed)
- [x] `hivepilot plugins packs list` / `info skills-kit`
