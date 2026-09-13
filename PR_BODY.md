## Summary

HP-102: one shared `decide_approval()` for Pollen cards and the Telegram Approvals-door keyboard. Same `approval_id` on both surfaces → a single resume. Owner + TTL via `pending_confirmation`. Four Telegram doors stay inbox | approvals | runs | alerts (no fifth topic). WhatsApp is out of scope (HP-129).

Owning issue: [HP-102](https://linear.app/js-workspace/issue/HP-102/u-08-presenter-pollen-telegram-parity-door-approvals)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-97 PASS and HP-101 `decide_memory`.

Replay: `pytest tests/test_presenters.py tests/test_telegram_pass_presenter.py tests/test_api_pass_approvals.py tests/test_telegram_doors.py tests/test_memory_proposals.py tests/test_pass_store.py tests/test_checkpoints.py tests/test_pending_confirmation.py`

## What changed

1. **`hivepilot/presenters.py`** — `present()` / `pollen_card()` / `telegram_keyboard()` / `decide_approval()`. Keyboard is `None` off the Approvals door.
2. **Telegram** — PASS callback `pass:<decision>:<approval_id>` and `/approvals` send PASS keyboards to the Approvals topic only.
3. **Pollen** — `GET/POST /v1/pass-approvals/{approval_id}` plus ApprovalsView PASS inbox (same `approval_id`).
4. **Docs** — SECURITY fail-closed checklist + ARCHITECTURE safety model.

## Out of scope

- WhatsApp (HP-129), HP-67 sandbox, a fifth Telegram topic, vendored TS / Electron / pickle

## Testing

- [x] `pytest tests/test_presenters.py tests/test_telegram_pass_presenter.py tests/test_api_pass_approvals.py tests/test_telegram_doors.py tests/test_memory_proposals.py tests/test_pass_store.py tests/test_checkpoints.py tests/test_pending_confirmation.py` — 99 passed
- [x] `pytest tests/test_telegram_bot.py tests/test_approval_forum_topic.py` — 103 passed (no regression)
- [x] `ruff check` + `ruff format --check` clean
- [x] Pollen Vitest: `pollen-api.test.ts` + `ApprovalsView.test.tsx` + i18n — 96 passed
