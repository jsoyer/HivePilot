## Summary

HP-53 first slice: **migrate mem0 memories into Hindsight**, start retirement, do not delete the plugin.

- New `hivepilot memory migrate-mem0` (`--dry-run`, `--user-id`, `--force`). Same bank key as HP-51: `{project}:{task}:{role}`. Each memory is `retain`ed with a `[migrated-from-mem0]` prefix (redacted).
- Idempotent log table `mem0_migration_log` in `state.db`. A second run skips already-copied ids. One retain failure does not abort the rest.
- Soft deprecation: `mem0.health()` and `plugins install` copy point at the new command. mem0 plugin, Search tab, `GET /v1/memories`, and historical `memory_events` stay.

Out of slice: delete `bundled_plugins/mem0.py`, remove Search tab / `/v1/memories`, drop `KNOWN_BACKENDS` mem0, honcho/obsidian (untouched).

Linear: [HP-53](https://linear.app/js-workspace/issue/HP-53/migrationretrait-de-mem0-bascule-des-memoires-existantes-suppression). Parent HP-32.

## Testing

- [x] `pytest tests/test_mem0_hindsight_migration.py tests/test_mem0.py tests/test_hindsight.py -q`
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: enable both backends, then `hivepilot memory migrate-mem0 --dry-run` then without `--dry-run`. After verify recall, set `HIVEPILOT_MEM0_ENABLED=false`.
