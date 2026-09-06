## Summary

HP-53 slice 2: retire the mem0 plugin and Pollen Search tab. Slice 1 (#631) already migrated memories into Hindsight banks.

Kept (migration source only):
- `hivepilot memory migrate-mem0`
- `hivepilot/services/mem0_hindsight_migration.py`
- `HIVEPILOT_MEM0_*` settings / `.env.example` comments
- historical `memory_events` rows (`_LEGACY_BACKEND = "mem0"`)
- `memoryBackends.about.mem0` i18n for leftover Quality/Sources cards

Removed:
- `hivepilot/bundled_plugins/mem0.py` and `tests/test_mem0.py`
- Pollen `Mem0View` + Memory Search tab (`GET /v1/memories`)
- TUI Mem0 tab
- plugin catalog / activity / doctor probes for mem0
- `memory_service.KNOWN_BACKENDS` entry (`obsidian`, `hindsight` only)

Does not collapse HP-51 `{project}:{task}:{role}` and HP-52 `role:{name}` banks. Disposition still unset.

Stacked on HP-55 (`cursor/hp-55-memory-panel-6c07` / #632) so this review is retirement-only.

Linear: [HP-53](https://linear.app/js-workspace/issue/HP-53/migrationretrait-de-mem0-bascule-des-memoires-existantes-suppression). Parent HP-32.

## Testing

- [x] `pytest` installer / doctor / dashboard / memory / pollen contract / gating / api_service (730+ passed; health alias isolated from host `plugins_disabled`)
- [x] `cd web && npm test -- --run src/components/views/MemoryView.test.tsx src/lib/pollen-api.test.ts src/components/Pollen.test.tsx` (90 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-mOBs-rFC.js`)
- [x] `hivepilot memory migrate-mem0 --help` — CLI remains; docstring says plugin is retired
- [x] `hivepilot plugins available` — no `mem0` catalog row
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: `hivepilot memory migrate-mem0 --dry-run` still lists a mem0 export; Pollen Memory has Sources / Knowledge / Quality / Growth (no Search).
