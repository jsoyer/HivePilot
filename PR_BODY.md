## Summary

HP-108: skill→tools gate for concierge/chat. A skill maps to HP-95 catalog tokens; when the skill is off (HP-105 `enabled=False` or unknown), those tokens are absent from the chat allowlist. Pipeline stage-attach is unchanged. No keyword force-load.

Owning issue: [HP-108](https://linear.app/js-workspace/issue/HP-108/u-14-skilltools-gate-conciergechat)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / [skills-first HP-103](https://linear.app/js-workspace/issue/HP-103/u-09-doctrine-skills-first-audit-top-5-runtimeskill) / plan `coworker-openspace`. Builds on HP-95 tool catalog and HP-105 `enabled`.

Replay: `pytest tests/test_skill_capabilities.py tests/test_skill_trust.py tests/test_skill_ranker.py tests/test_skill_orchestrator_wiring.py tests/test_cli_config_commands.py -k attach`

## What changed

1. **`hivepilot/skill_capabilities.py`** — Coworker `skill-capabilities.ts` rewritten. Map skill → tokens (`SKILL.md` `allowed-tools`, overlay, bundled `improve` default only when that skill is cataloged). `resolve_chat_tools` drops tokens whose owners are all off. `chat_tool_surface` / `on_chat_surface` mark concierge/chat runs so `_role_runner_options` filters; pipelines pass through.
2. **Concierge/chat wiring** — `resolve_role_chat_tools` on the concierge service. ChatOps / Telegram / Discord / Slack `run_task` / `run_pipeline` run under the chat surface. Classifier stays `--tools ""`.
3. **Docs** — SKILLS / SECURITY / ARCHITECTURE / pipelines / skills-first ADR note the chat-only gate and unchanged stage-attach.

## Out of scope

- HP-109 FIX/DERIVED/CAPTURED apply, HP-112 host skills, HP-114 hybrid RRF
- WhatsApp, HP-67 sandbox, vendored Coworker TS / Electron
- Changing `PipelineStage.skills` or `hivepilot stage attach-skill`

## Testing

- [ ] `pytest tests/test_skill_capabilities.py` plus related skill / attach suites
- [ ] `ruff check` + `ruff format --check` on touched Python
