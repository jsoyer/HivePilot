## Summary

HP-130d — deploy packaging for multi-door Telegram bots. Keep **one** `hivepilot-telegram` systemd/OpenRC unit: HP-130b already polls N Applications in that process when 2+ distinct door tokens are set. Four units would fight on `getUpdates` or silently stay on the legacy forum-topic path.

Env examples document `HIVEPILOT_TELEGRAM_BOT_TOKEN_{INBOX,APPROVALS,RUNS,ALERTS}` in **shared.env** (api / scheduler / telegram must agree — `telegram_multi_token_mode()` is per-process). Live cutover needs Jerome’s four BotFather tokens (not included). **No automatic cutover.** Leftover forum topics 2118–2121 remain until HP-130e.

Owning issue: [HP-130](https://linear.app/js-workspace/issue/HP-130/4-door-bots-telegram-inboxapprovalsrunsalerts) (slice 130d)

Builds on HP-130a (`eefd90c` / PR #694), HP-130b (`46cbcc9` / PR #695), HP-130c (`b02bf70` / PR #696).

Replay: `hivepilot run example-api docs --dry-run`

## What changed

1. **systemd / OpenRC** — door-token placeholders + no-cutover notes on `hivepilot-telegram` env examples and `shared.env.example`. READMEs: enable multi-token vs keep single-token legacy; why one unit; restart api+scheduler+telegram together.
2. **setup-openrc.sh** — never prompts for door tokens. If already set in the environment, writes them into every generated `conf.d` (env silo).
3. **Kustomize / Helm** — cheap comments only: one telegram Deployment; optional door keys in the shared Secret.
4. **Docs** — INTEGRATIONS, DEPLOYMENT, DEPLOY-PRODUCTION, CLI-REFERENCE, SECURITY.

## Out of scope

- 130e cutover flag / wipe registry / delete Telegram topics 2118–2121
- Changing production noxysdevbot env or restarting services
- Creating `run:{id}` topics
- Four systemd/OpenRC units (wrong fit for 130b)

**Warning:** do not live-cutover without Jerome’s four BotFather tokens.

## Testing

- [x] `pytest tests/test_deploy_systemd_templates.py tests/test_deploy_openrc_templates.py tests/test_setup_openrc.py` — 58 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python; `sh -n scripts/setup-openrc.sh`
- [ ] `hivepilot lint` — pre-existing missing project paths in this env (`/home/ubuntu/dev/example-api` …), not caused by this slice
