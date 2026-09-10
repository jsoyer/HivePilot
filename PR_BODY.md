## Summary

Close the P7 sandbox-computers spike (HP-39) by finishing the leftover children:

- **HP-67** — decision is **no-go**. Docker / E2B / Daytona / Box were evaluated. CLI `bwrap` is not a desktop. HivePilot does not invent a `SandboxProvider` or a fleet of sandboxes. Encoded as `GET /v1/sandbox/provider`.
- **HP-72** — take-over / hand-back / skip are **fail-closed**. No sandbox desktop is attached, so control is refused (not bound to this host). Encoded as `GET /v1/computer/session` + `POST /v1/computer/{takeover,handback,skip}`. Home shows the honest empty copy, not working Take over buttons.

Linear: [HP-67](https://linear.app/js-workspace/issue/HP-67/spike-de-decision-archi-provider-sandbox-dockere2b-surface-securite), [HP-72](https://linear.app/js-workspace/issue/HP-72/takeover-handback-controle-dordinateur-par-lhumain-give-me-the-control), parent [HP-39](https://linear.app/js-workspace/issue/HP-39/sandbox-computers-spike).

Replay: Home → This host → Sandbox computer.

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp67_hp72_sandbox_computers.py tests/test_pollen_contract.py -q` — 30 passed
- [x] `cd web && npm test -- --run src/components/views/HomeView.test.tsx src/lib/pollen-api.test.ts src/components/Pollen.test.tsx` — 119 passed
