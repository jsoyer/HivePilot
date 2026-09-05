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

- [ ] `pytest tests/test_mem0_hindsight_migration.py tests/test_plugin_activity.py tests/test_plugin_installer.py tests/test_config_doctor.py tests/test_dashboard.py tests/test_dashboard_panels.py tests/test_api_memory.py tests/test_memory_service.py tests/test_pollen_contract.py tests/test_gating_conformance.py -q`
- [ ] `cd web && npm test -- --run src/components/views/MemoryView.test.tsx src/lib/pollen-api.test.ts`
- [ ] `cd web && npm run build` (Node 26.5.0)
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: `hivepilot memory migrate-mem0 --dry-run` still lists a mem0 export; Pollen Memory has Sources / Knowledge / Quality / Growth (no Search).
