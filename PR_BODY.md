## Summary

HP-87: OpenCode Go (`https://opencode.ai/zen/go/v1`) returns `400 MissingSessionID` unless the OpenAI-compat request carries `x-opencode-session` (stable per conversation) and a non-generic `User-Agent`. Production noxysdevbot had a temporary hotpatch; this replaces it in-repo.

`PromptCliRunner._run_api`'s `openai` branch now adds those headers when the base URL contains `opencode.ai`. Session id resolution is env (`HIVEPILOT_OPENCODE_SESSION` / `OPENCODE_SESSION`), then concierge `conversation_id` / payload metadata, then the stable CLI default `hivepilot-concierge`. `concierge_service.route` threads `conversation_id` into `runner_env` when the runner is `openai`. Non-OpenCode endpoints stay Authorization-only.

Owning issue: [HP-87](https://linear.app/js-workspace/issue/HP-87/opencode-go-send-x-opencode-session-on-concierge-openai-compat-runner) (follow-up to HP-18).

Replay: `hivepilot run example-api docs --dry-run`

## Testing

- [x] `pytest tests/test_prompt_cli_runner.py tests/test_openai_runner.py tests/test_concierge_service.py -q`
- [x] `hivepilot lint`
