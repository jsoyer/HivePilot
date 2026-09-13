## Summary

HP-93: P0 Telegram stop-bleed. Keep the current four-door forum (Inbox / Approvals / Runs / Alerts). Do **not** remint doors or mint `run:{id}` topics — the Bot API cannot list or dedupe names, so recreate-on-miss + startup `ensure_pollen_doors` + ephemeral run topics were the bleed.

Owning issue: [HP-93](https://linear.app/js-workspace/issue/HP-93/p0-telegram-stop-bleed-no-run-topics-no-door-remint)

Replay: `pytest tests/test_telegram_stop_bleed.py tests/test_stream_topics.py tests/test_notification_service.py tests/test_approval_forum_topic.py tests/test_topics_admin.py tests/test_telegram_doors.py tests/test_telegram_bot.py`

## What changed

1. **No `createForumTopic` for `run:{id}`** — live turns stay index-only on the persistent Runs door.
2. **Wipe-sync registry** — dead ids drop JSON **and** the SQLite mirror (`_invalidate_topic`). After an operator wipe: `hivepilot topics wipe-sync --yes` then `hivepilot topics bootstrap --yes`.
3. **Recreate-on-stale OFF for persistent doors** — fallback Inbox, then DM. Never remint Inbox/Approvals/Runs/Alerts.
4. **`ensure_pollen_doors` is a no-op** when any door is already registered (including a partial set). Empty mint only via `hivepilot topics bootstrap --yes`.
5. **File lock** around remaining create+register (bootstrap / leftover role keys) so api / telegram / scheduler cannot race two `createForumTopic` calls.

## P1 follow-up (explicitly out of scope)

- Four separate bot tokens / DMs
- `STREAM_TOPICS=false` settings map
- Wiping the old forum from HivePilot
- ~20 bots per role (NO-GO)

## Testing

- [x] `pytest` Telegram / topic suites — 227 passed
- [x] `ruff check` + `ruff format --check` clean
