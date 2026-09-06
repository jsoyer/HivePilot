## Summary

HP-68: rebase slice 1 (this-host RAM/CPU/disk + Paul-style spend ranks) onto current main, then add an honest **this-host process list** and **loopback browser tabs**.

Kept only what HivePilot can measure:

- Last 24h by provider on Home
- By-model ranked list on Models
- This host RAM / CPU / disk (`GET /v1/host/resources`)
- This host allowlisted agent processes (`GET /v1/host/processes`)
- Real browser tabs only when a loopback Chrome DevTools endpoint answers (`GET /v1/host/browser`)

Not invented: quota % / runway, a fake “N servers” count, remote CDP, a full `ps aux` dump.

Linear: [HP-68](https://linear.app/js-workspace/issue/HP-68/prototype-panneau-ressources-ramcpussd-serveurs-actifs-navigateur).

Replay: Home → This host. Processes come from `/proc`. Tabs appear only if `HIVEPILOT_BROWSER_CDP_URL` points at loopback (default `http://127.0.0.1:9222`).

## Testing

- [ ] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp68_host_resources.py tests/test_pollen_contract.py -q`
- [ ] `cd web && npm test -- --run src/components/views/HomeView.test.tsx`
