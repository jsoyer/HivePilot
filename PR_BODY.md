## Summary

HP-130a — config-only door → bot token map for Inbox / Approvals / Runs / Alerts. Optional per-door env vars fall back to the existing shared `HIVEPILOT_TELEGRAM_BOT_TOKEN` so a single-bot deploy is unchanged. No multi-bot polling, topic routing, systemd split, or STREAM_TOPICS cutover.

Owning issue: [HP-130](https://linear.app/js-workspace/issue/HP-130/4-door-bots-telegram-inboxapprovalsrunsalerts) (slice 130a)

Replay: `pytest tests/test_telegram_doors.py tests/test_settings_secret_repr.py -q`

## What changed

1. **`Settings`** — `telegram_bot_token_{inbox,approvals,runs,alerts}` (`HIVEPILOT_TELEGRAM_BOT_TOKEN_INBOX` …). Secret-typed (repr / `config get` masked). Four env vars, not a JSON map, so systemd `EnvironmentFile` stays `KEY=value`.
2. **`telegram_doors`** — `telegram_bot_token_for_door` / `telegram_door_bot_tokens` resolve door-specific token → `telegram_bot_token` → `TELEGRAM_BOT_TOKEN`. Blank overrides and unknown keys use the shared fallback.
3. **Docs** — `.env.example`, systemd/OpenRC telegram env examples, INTEGRATIONS / SECURITY / CLI-REFERENCE.

## Out of scope (later slices)

- 130b multi-Application polling
- 130c route without `message_thread_id`
- 130d systemd multi-unit deploy
- 130e cutover / disable `STREAM_TOPICS`
- Creating `run:{id}` forum topics — P0 stop-bleed stays intact

## Live registry (noxysdevbot) — 130a does not wipe

Forum doors remain until 130e. Live registry still has `inbox=2118` … `alerts=2121` (and `_inbox_welcome=2123`) under `/var/lib/hivepilot/data/hivepilot/stream_topics.json`. `hivepilot topics list` can falsely show empty without `HIVEPILOT_BASE_DIR=/var/lib/hivepilot/data`. Orphan forum topics stay until cutover.

## Testing

- [x] `pytest tests/test_telegram_doors.py tests/test_settings_secret_repr.py` — fallback, per-door override, env mapping, secret mask
- [ ] `hivepilot lint` — run after commit
