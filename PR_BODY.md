## Summary

**HP-16** — Telegram hand-offs prefix a per-role avatar.

- Unicode fallback for the eight first-class roles (same keys as Pollen HP-20): CEO 👑, CoS 📋, CTO 🧭, Developer 🛠️, Reviewer 🔍, CISO 🛡️, QA 🧪, Documentation 📝.
- Optional `telegram_avatars.yaml` maps `role → custom_emoji_id`. HTML cards emit `<tg-emoji>`; the plain path attaches a `custom_emoji` MessageEntity (UTF-16 offset). `parse_mode` and `entities` stay mutually exclusive.
- Gate: `HIVEPILOT_TELEGRAM_CUSTOM_EMOJI` (default on). Missing IDs, unknown actors, a disabled flag, or a rejected send (no Premium / stale id) degrade to the Unicode glyph.
- This environment does **not** upload a sticker set — ops via `@Stickers` (see `docs/INTEGRATIONS.md`).

Linear: [HP-16](https://linear.app/js-workspace/issue/HP-16/role-avatars-in-telegram-via-custom-emoji-premium).

Replay: `hivepilot run example-api docs --dry-run` (stream path is unit-tested; no live Telegram).

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_telegram_avatars.py tests/test_telegram_formatting.py tests/test_notification_service.py tests/test_stream_topics.py tests/test_config_validation.py tests/test_telegram_channel.py tests/test_telegram_chunking.py tests/test_stream_send_is_recorded.py tests/test_stream_fanout.py -q` — 167 passed
- [x] `hivepilot validate` — OK (pre-existing pipeline input warnings only)
- [x] `hivepilot lint` — pre-existing missing `~/dev/*` project paths only
- [x] `ruff format --check` on touched Python — clean
- [x] CI follow-up: `telegram_avatars.yaml` added to `config_service.CONFIG_FILES` (sync roster); fixture typed as `Iterator[None]` for mypy
