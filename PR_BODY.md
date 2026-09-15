## Summary

HP-130c — route Inbox/Approvals/Runs/Alerts by door-bot identity when 2+ distinct tokens are configured. Stream and notifications pick the door via `telegram_bot_token_for_door` / door classification and omit forum `message_thread_id`. A single shared token keeps the current `telegram_stream_topics` path unchanged until 130e cutover. Approvals keyboards stay Approvals-only. P0 stands: no `run:{id}` createForumTopic.

Owning issue: [HP-130](https://linear.app/js-workspace/issue/HP-130/4-door-bots-telegram-inboxapprovalsrunsalerts) (slice 130c)

Builds on HP-130a (`eefd90c` / PR #694) and HP-130b (`46cbcc9` / PR #695).

Replay: `hivepilot run example-api docs --dry-run`

## What changed

1. **`telegram_multi_token_mode()`** — true when `telegram_door_token_groups()` has 2+ distinct tokens.
2. **`notification_service`** — `_send_telegram` / `_notify_telegram` / stream path use the door token and drop `message_thread_id` in multi-token mode. `door_thread` / `ensure_pollen_doors` / `_ensure_topic_thread` no-op so leftover topics 2118–2121 are unused. Single-token + `STREAM_TOPICS` still looks up the registry.
3. **`telegram_bot`** — inbound challenge/concierge keys use `_door_of`; Approvals commands and keyboards refuse non-Approvals apps; outbound approvals use the Approvals token and never attach a thread id.
4. **`TelegramStreamChannel`** — multi-token ensure/send is threadless and tagged `door=runs`.

## Out of scope

- 130d systemd multi-unit
- 130e cutover flag / wipe registry / delete orphan topics 2118–2121
- Webhook multi-bot

## Testing

- [x] `pytest tests/test_telegram_doors.py tests/test_telegram_bot.py tests/test_telegram_channel.py tests/test_stream_topics.py tests/test_approval_forum_topic.py tests/test_telegram_stop_bleed.py tests/test_notification_service.py`
- [x] `hivepilot lint` (or the subset of ruff on touched Python)
