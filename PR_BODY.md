## Summary

HP-51 first slice: **Hindsight as a memory substrate** — `recall` before a step, `retain` after — without embedding Hindsight's engine in the HivePilot worker.

- Bundled plugin `hindsight.py`: HTTP client (`hindsight-client`) onto a server the operator deploys (Docker / `hindsight-api` / Cloud on Postgres+pgvector). Dormant unless `HIVEPILOT_HINDSIGHT_ENABLED`.
- ADDITIVE recall (composes with honcho + obsidian). Bank id = `project:task:role`. Untrusted review payloads are skipped. Output is redacted before `retain`.
- Quality instrumentation (`backend="hindsight"`) + Memory backends panel slot. Egress is loopback=false / remote=true.
- Optional extra: `pip install "hivepilot[hindsight]"`.

Out of slice: `reflect()` (HP-54), mem0 migration (HP-53), role-as-bank (HP-52).

Linear: [HP-51](https://linear.app/js-workspace/issue/HP-51/integrer-hindsight-comme-substrat-memoire-retainrecall-dans-les-hooks). Parent HP-32.

## Testing

- [x] `pytest tests/test_hindsight.py tests/test_gating_conformance.py tests/test_api_memory.py tests/test_plugin_installer.py tests/test_bundled_plugins_layout.py tests/test_config.py::TestHindsightEnabled -q` — 311 passed
- [x] `npm test` — MemoryBackendsView (5)
- [x] `npm run build` — Pollen static (i18n + backends card)
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: `export HIVEPILOT_HINDSIGHT_ENABLED=true` and point `HIVEPILOT_HINDSIGHT_BASE_URL` at a running Hindsight (`http://127.0.0.1:8888`). Dry-run still no-ops if the client extra is missing.
