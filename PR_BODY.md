## Summary

HP-101: every model memory write is a PASS proposal (`kind=memory`). Never silent. No Always-allow. Pending/reject leave IsolatedJsonMemory and workspace text unchanged. Edit stages user text; approve applies it. Recall is zero-approval. User Pollen/vault writes go through `user_write` (direct).

Owning issue: [HP-101](https://linear.app/js-workspace/issue/HP-101/u-07-memory-proposals-hitl-proposeapproveeditreject)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-96 isolated memory, HP-97 PASS, HP-100 side_effects for apply-once.

Replay: `pytest tests/test_memory_proposals.py tests/test_pass_store.py tests/test_workspace_text.py tests/test_side_effects.py`

## What changed

1. **`hivepilot/memory_proposals.py`** — propose / decide (approve, edit, reject) / recall / `user_write`. Apply IsolatedJsonMemory or workspace text only after APPROVED.
2. **`hivepilot/pass_store.py`** — `stage_edit` merges user text while status stays PENDING (`decide('edit')` remains terminal and is not an apply signal).
3. **Docs** — SECURITY fail-closed checklist + ARCHITECTURE safety model.

## Out of scope

- HP-102 presenter parity (Pollen ↔ Telegram cards)
- HP-105 trust, WhatsApp, HP-67 sandbox, vendored TS / Electron / pickle

## Testing

- [ ] `pytest tests/test_memory_proposals.py tests/test_pass_store.py tests/test_workspace_text.py tests/test_side_effects.py`
- [ ] `ruff check` + `ruff format --check` clean
