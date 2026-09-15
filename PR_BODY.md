## DRAFT / DO NOT LIVE CUTOVER

**Do not merge-or-deploy this as a live noxysdevbot cutover.** Code can land; the operational wipe is gated on Jerome’s four BotFather tokens + Gaspard CoS GO. This PR does not change production env, does not restart services, and does not call Telegram `deleteForumTopic` against the live group.

Owning issue: [HP-130](https://linear.app/js-workspace/issue/HP-130/4-door-bots-telegram-inboxapprovalsrunsalerts) (slice **130e**)

Builds on HP-130a (`eefd90c` / #694), HP-130b (`46cbcc9` / #695), HP-130c (`b02bf70` / #696), HP-130d (`6690e46` / #697).

Replay: `pytest tests/test_telegram_cutover.py tests/test_topics_admin.py tests/test_telegram_stop_bleed.py tests/test_telegram_doors.py -q`

## Summary

HP-130e — explicit, safe cutover/cleanup after multi-token door bots (130c). When 2+ distinct door tokens are set, prefer door-bot routing (already shipped) and forget leftover forum topic ids locally. Legacy single-token `STREAM_TOPICS` stays until operators set the four tokens.

## What changed

1. **`hivepilot topics cutover`** — plan by default; `--yes` wipes JSON + SQLite (same `wipe_topic_registry` / file lock / mirror drop as P0 `wipe_sync`). **Refuses** unless `telegram_multi_token_mode()` is true, so a live STREAM_TOPICS registry cannot be wiped by accident. Never calls Telegram. Never runs on deploy/restart.
2. **`topics bootstrap`** — refuses to mint in multi-token mode (no `createForumTopic`).
3. **`topics wipe-sync`** — follow-up hint skips bootstrap when multi-token; still points at remint on the legacy path.
4. **Doctor** — `stale_forum_registry_multi_token` info finding when multi-token + leftover registry (hint only; never wipes).
5. **Docs** — INTEGRATIONS cutover checklist: set shared.env → restart api+scheduler+telegram together → `topics cutover --yes` → delete orphan topics **2118–2121** in the Telegram client or `topics prune 2118 2119 2120 2121 --yes`. Documented how to read `message_thread_id` from the topic link.

## Safety rails (unchanged + tested)

- No automatic live cutover on deploy/restart (`_build_application` / `run_polling` do not wipe or prune).
- No `createForumTopic` for `run:{id}` (P0 intact).
- No BotFather tokens in the repo.
- Orphan topic delete is a documented manual/CLI step, not delete-on-boot.

## Out of scope

- Changing production noxysdevbot env or restarting live units
- Four systemd units (130d chose one)
- Webhook multi-bot
- Automatic Bot API delete of 2118–2121

## Testing

- [x] `pytest tests/test_telegram_cutover.py tests/test_topics_admin.py tests/test_telegram_stop_bleed.py tests/test_telegram_doors.py tests/test_doctor_liveness.py tests/test_deploy_systemd_templates.py tests/test_deploy_openrc_templates.py tests/test_stream_topics.py tests/test_telegram_channel.py tests/test_approval_forum_topic.py` — 213 passed
- [x] `ruff check` clean on touched Python
