## Summary

HP-92: Align Telegram ChatOps with the Pollen four doors (PLAN §5). Persistent forum topics are **Inbox · Approvals · Runs · Alerts** — the same names as the web shell. General is no longer a catch-all: unmatched / closed / threadless traffic in the stream forum prefers Inbox (Alerts when the payload is failed / degraded / classifier). Runs is the run index; ephemeral RUN topics use `{emoji} {slug}` with the run id in the first message. Cards are softer (bold title + two meta lines). ACTION still uses the fail-closed Yes/No keyboard; ANSWER never gets buttons. System/concierge uses 🐝; hand-offs keep the role-charte emojis (no new pairs, no role color in chrome).

Owning issue: [HP-92](https://linear.app/js-workspace/issue/HP-92/pollen-redesign-pr5-telegram-4-topics-inboxapprovalsrunsalerts)

Replay: `pytest tests/test_telegram_doors.py tests/test_telegram_bot.py tests/test_approval_forum_topic.py tests/test_stream_topics.py tests/test_telegram_formatting.py tests/test_notification_service.py tests/test_topic_naming_convention.py tests/test_doctor_liveness.py tests/test_notifier_registry.py tests/test_telegram_avatars.py`

This slice closes the Pollen redesign Telegram follow-up (PR1–PR4 were web-only).

## Residual Telegram debt

- Telegram itself always creates a built-in **General** topic; we cannot delete it. HivePilot no longer dumps there, but last-resort threadless send still exists if Inbox cannot be created.
- Per-role persistent topics are no longer minted. Existing `Gustave (Developer)` registry rows remain until the operator prunes them (`hivepilot topics prune`).
- Slack / Discord stream threads are unchanged (not this PR).
- Approval checkpoint *details* still follow the card (operators need the plan); only the header is the soft 3-line shape.
- Optional read-only Welcome topic was not added — Inbox gets a pinned welcome instead.

## Testing

- [x] `pytest` Telegram / topic / concierge / doctor / chatops suites — 347 passed
- [x] `hivepilot lint` — no new Telegram/config errors (pre-existing missing example project paths in this environment)
