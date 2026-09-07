## Summary

HP-82: treat OpenCodex (`ocx`) as its own local **provider proxy**, not as Codex and not as OpenCode.

Three different things stay three different things:

- `codex` — OpenAI Codex CLI runner / CLI sign-in (`cli` on `/v1/onboarding/machine`)
- `opencode` — OpenCode CLI runner
- **OpenCodex** — loopback proxy (`ocx`, default `http://127.0.0.1:10100`) under a new `proxies` list

HivePilot does **not** register `kind: opencodex` as a runner, does **not** write `~/.codex/config.toml`, and does **not** offer a “Codex via OpenCodex” toggle. Providers → On this machine shows OpenCodex in **Local proxies**, with no CLI login and no Verify-as-Codex.

Linear: [HP-82](https://linear.app/js-workspace/issue/HP-82/opencodex-ocx-comme-proxy-local-distinct-de-codex).

Replay: `GET /v1/onboarding/machine` — `proxies[].kind` is `opencodex`, `cli` still has `codex`. Open Providers and confirm the proxy row is not a CLI session.

## Testing

- [ ] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp82_opencodex_probe.py tests/test_api_service.py::TestOnboardingMachine -q`
- [ ] `cd web && npm test -- --run src/components/views/ProvidersView.test.tsx`
