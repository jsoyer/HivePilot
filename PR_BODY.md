## Summary

Paul-style spend density on Home / Models, plus an honest this-host resource strip (HP-68 slice 1).

From the Hublot/AO mockups we kept only what HivePilot can actually measure:

- **Last 24h by provider** on Home — compact tokens (k / M / B) + cost, labeled as a rolling 24h window (not calendar “today”)
- **By model** ranked list on Models — provider mark, compact tokens, cost (the detailed table stays)
- **This host** RAM / CPU / disk from Linux `/proc` + `shutil.disk_usage` (`GET /v1/host/resources`)

Intentionally **not** copied:

- Provider quota % / runway days (needs a provider quota API — same honesty as HP-73)
- A “servers” count (no inventory)
- Real browser tabs / process list (rest of HP-68; threat model still open)

Linear: [HP-68](https://linear.app/js-workspace/issue/HP-68/prototype-panneau-ressources-ramcpussd-serveurs-actifs-navigateur) slice 1 only.

Replay: open Pollen Home after `hivepilot api` — last-24h list uses `GET /v1/analytics/cost?days=1`; host strip uses `GET /v1/host/resources`.

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp68_host_resources.py tests/test_pollen_contract.py -q`
- [x] `cd web && npm test -- --run src/lib/format-usage.test.ts src/lib/pollen-api.test.ts src/components/views/HomeView.test.tsx src/components/views/ModelsView.test.tsx src/components/views/ProvidersView.test.tsx src/components/dashboard/ProviderMark.test.tsx src/components/Pollen.test.tsx`
