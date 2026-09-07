## Summary

HP-83: **use** OpenCodex as a local OpenAI-compat backend. HP-82 only discovered it.

- Probe lists models from loopback `GET /v1/models`
- `POST /v1/models/verify` accepts `provider: opencodex` (never `agent_kind: codex`)
- A prompt-CLI step with `mode: api` + `api_provider: opencodex` POSTs `/v1/chat/completions` to `http://127.0.0.1:10100/v1`
- Still no `kind: opencodex` runner, no `~/.codex/config.toml`, no Codex→OpenCodex fallback

Linear: [HP-83](https://linear.app/js-workspace/issue/HP-83/utiliser-opencodex-comme-backend-openai-compat-local).

Replay: `hivepilot run …` with a step `options.mode: api` / `api_provider: opencodex`, or Providers → Local proxies → Verify.

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp83_opencodex_use.py tests/test_hp82_opencodex_probe.py tests/test_model_verify.py tests/test_prompt_cli_runner.py::TestApiModeCaptureUsage -q`
- [x] `cd web && npm test -- --run src/components/views/ProvidersView.test.tsx`
