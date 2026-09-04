## Summary

HP-65 first slice: **verify then save** a cloud API key, plus CLI Sign-in on Providers. Native OpenRouter/OpenAI OAuth and gemini device-code stay later.

- `model_connect.connect` — fail closed: no write unless `model_verify.verify` returns `ok`. Upserts the provider env var into the resolved `.env` (`0600`). Never echoes the key. Local daemons (Ollama / LM Studio) have nothing to persist.
- `POST /v1/models/connect` — admin + `consent: true`. Audit row stores a SHA-256 fingerprint, never the secret. SSRF `base_url` is refused.
- `hivepilot model connect` — same path; `--env-file` for tests.
- Providers: **Connect a model** form (admin) and **Sign in** on absent CLI sessions (`login_available`).

Linear: [HP-65](https://linear.app/js-workspace/issue/HP-65/onboarding-connecter-un-modele-openrouteroauthdevice-codeopenai-compat). Builds on HP-78.

## Testing

- [x] `pytest tests/test_model_connect.py tests/test_api_service.py::TestModelsConnect -q` — 15 passed
- [x] `npm test` — 843 passed (ProvidersView connect/login + pollen-api)
- [x] `npm run build` — Pollen static rebuilt (`hivepilot/webui/static`)
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths in this env (not caused by this PR)

Replay: `hivepilot model connect --provider openai --api-key <key>` (writes `.env` after a live `GET /models`). Read-only check: `hivepilot model verify --provider openai`.
