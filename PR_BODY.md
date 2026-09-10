## Summary

Config hygiene + right-size routing, no HP-18 overlap.

- **HP-17** — Company-pipeline tasks in `tasks.yaml` now declare the same agent kind as `roles.yaml`. Stale `runner: claude` / `runner_ref: claude-*` / `metadata.claude_profile` are gone. `roles.yaml` stays authoritative for kind/model. `hivepilot validate` reports drift when a role-bound agent step's `runner` or named `runner_ref` kind disagrees.
- **HP-21** — Dual-model debate is **opt-in** (`Role.debate`, default `false`). CEO keeps both models for `hivepilot debate` but `ceo-intake` / `ceo-approval` no longer auto-debate. QA (Marie) drops one tier: `model_profile: automation` (cursor → `composer-2.5-fast`). Developer / CTO / Reviewer / CISO unchanged.

Linear: [HP-17](https://linear.app/js-workspace/issue/HP-17/config-hygiene-align-tasksyaml-runner-defs-with-rolesyaml), [HP-21](https://linear.app/js-workspace/issue/HP-21/right-size-model-routing-ceo-debate-opt-in-qa-one-tier-down).

Replay: `hivepilot validate`. Company task runners now match `roles.yaml`; `ceo-intake` no longer auto-debates.

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp17_role_task_align.py tests/test_lint_service.py tests/test_role_runner_binding.py tests/test_roles_store.py tests/test_stage_model_effort.py tests/test_roles_config_owned.py tests/test_company_pipeline.py tests/test_orchestrator.py tests/test_config_validation.py tests/test_roles_model_effort_integration.py tests/test_roles.py tests/test_roles_api.py -q` — 327 passed
- [x] `hivepilot lint` — no unknown-runner errors (remaining findings are pre-existing missing `~/dev/*` project paths)
- [x] `hivepilot validate` — OK, no role-runner drift
