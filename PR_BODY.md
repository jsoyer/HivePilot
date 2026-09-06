## Summary

HP-71: Hermes-4 as an OpenAI-compat **model** (Nous Portal / OpenRouter). The Hermes Agent framework is not embedded.

- `model_profiles.yaml` adds `openrouter:` / `nous:` columns plus dedicated `hermes-4` and `hermes-4-405b` profiles.
- `api_provider: nous` posts to `https://inference-api.nousresearch.com/v1/chat/completions` (`NOUS_API_KEY`).
- Providers panel + `POST /v1/models/verify|connect` accept `nous` (SSRF allowlist includes `inference-api.nousresearch.com`).
- Default `HIVEPILOT_DEV_FALLBACK_RUNNERS` is `codex`, `cursor`, `openrouter`. A developer quota/credit miss falling over to OpenRouter gets the profile's Hermes-4 slug and `mode: api` (not the originating Claude alias).

Linear: [HP-71](https://linear.app/js-workspace/issue/HP-71/integrer-hermes4-comme-providermodele-nous-portal-openrouter).

Replay: `hivepilot run example-api docs --dry-run` (profiles only). Live check: `hivepilot` model verify `nous` with `NOUS_API_KEY`, or OpenRouter with `OPENROUTER_API_KEY`.

## Testing

- [x] `pytest tests/test_hermes4_profiles.py tests/test_model_verify.py tests/test_model_connect.py tests/test_local_models.py tests/test_prompt_cli_runner.py tests/test_quota_fallback.py tests/test_model_profiles_single_source.py` (74 passed)
- [x] `cd web && npm test -- --run src/components/views/ProvidersView.test.tsx` (8 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-C0GPgnZk.js`)
- [x] `ruff check` on touched Python
