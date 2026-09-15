## Summary

HP-130b — runtime multi-Application Telegram polling. Builds on HP-130a (`eefd90c` / PR #694): when two or more distinct door tokens resolve from `telegram_bot_token_for_door` / `telegram_door_bot_tokens`, `hivepilot telegram start` (polling) runs one python-telegram-bot Application per unique token, each bound to the door(s) that use it. Shared handlers are parameterized by `door`. A single shared token keeps the current one-bot path.

Owning issue: [HP-130](https://linear.app/js-workspace/issue/HP-130/4-door-bots-telegram-inboxapprovalsrunsalerts) (slice 130b)

Replay: `pytest tests/test_telegram_doors.py tests/test_telegram_bot.py tests/test_telegram_ask.py -q`

## What changed

1. **`telegram_doors.telegram_door_token_groups`** — groups `PERSISTENT_DOORS` by distinct BotFather token (door order preserved; doors with no token omitted).
2. **`telegram_bot.run_polling`** — `len(groups) <= 1` still calls `_token()` + `_build_application(token)` + `app.run_polling(drop_pending_updates=True)`. Two or more groups build one Application each (`_build_application(token, doors=…)`) and poll them on one loop (`_run_polling_many`).
3. **Shared handlers** — `_shared_handler` / `_bind_application_doors` / `_door_of` parameterize the existing command/message/callback handlers by door. Single-bot path passes `doors=None` so the registered functions are unchanged.
4. **Docs** — INTEGRATIONS, CLI-REFERENCE, SECURITY, `.env.example`, systemd/OpenRC telegram env examples. Webhook stays single-token.

## Out of scope (later slices)

- 130c route without `message_thread_id` / ignore STREAM_TOPICS for doors
- 130d systemd multi-unit
- 130e cutover / wipe topics
- Creating `run:{id}` forum topics — P0 stop-bleed stays intact
- Webhook / FastAPI multi-bot

## Testing

- [ ] `pytest tests/test_telegram_doors.py tests/test_telegram_bot.py tests/test_telegram_ask.py` — grouping, single-token path unchanged, multi-token builds N apps / correct door binding
- [ ] `ruff check` on touched Python
- [ ] `hivepilot lint` if the env has the CLI
