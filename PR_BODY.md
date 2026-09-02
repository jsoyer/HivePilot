## Summary

Chloe (Ecurie / Stargate) wired Langfuse OSS onto LiteLLM. One `litellm-pass_through_endpoint` trace was **296,865 prompt tokens / $1.4861**. HivePilot already meters those envelopes on `steps` — Cost and Providers then fold them into `claude · 30d`.

This PR surfaces **top-N whale steps** from data we already store. It does **not** ingest Langfuse/LiteLLM full traces (Langfuse itself truncates those payloads).

- `analytics_service.cost_whales` — `steps JOIN runs`, exclude shell/container/`skip:`, order by effective `cost_usd` then `input_tokens`, default limit 20.
- `GET /v1/analytics/whales?days=7&limit=20` — tenant-scoped like `/v1/analytics/cost`. Envelopes only: no prompt bodies, no invented latency.
- Cost tab: **Largest steps** table, same 1/7/30 window.

Linear: [HP-81](https://linear.app/js-workspace/issue/HP-81/whale-steps-surface-the-dollar150-300k-token-calls-on-cost). Related: HP-73.

## Testing

- [ ] `pytest tests/test_analytics_service.py tests/test_api_service.py tests/test_pollen_contract.py -q`
- [ ] `npm test` (`CostView.test.tsx`, `pollen-api.test.ts`, `Pollen.test.tsx`)
- [ ] `hivepilot lint`

Replay: `hivepilot run example-api docs --dry-run` (unchanged). New read: `GET /v1/analytics/whales`.
