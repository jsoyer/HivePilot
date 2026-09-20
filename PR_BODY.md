Owning issue: [HP-119](https://linear.app/js-workspace/issue/HP-119/r1-pollen-onglet-conversations-interactions-actortarget)

Replay: `cd web && npm test -- src/components/views/RunDetailPanel.test.tsx src/components/views/ConversationMessageRow.test.tsx src/components/views/RunBoardView.test.tsx src/components/views/ConversationsView.test.tsx`

## Summary

HP-119 — per-run Conversations inside the existing Runs shell. Open a run from Board or History; the detail drawer now has **Steps | Conversations**. Conversations is not a fifth top-level door.

The fil is `GET /v1/conversations/{run_id}`: oldest-first actor→target turns, role from `interactions.metadata.role`, outputs in collapsible `<details>`. The API now also returns `target`.

## What changed

1. **Run detail** — Steps / Conversations pills (same surface-tab pattern as Board / History). Thread loads only when Conversations is opened.
2. **Shared fil UI** — `ConversationFil` / `ConversationMessageRow` reused by the run drawer and the existing ⌘K Conversations lab view.
3. **API** — `Message.target` from `interactions.target`.
4. **Tests + fixture** — `web/src/test/fixtures/interactions.ts` seeds a three-turn actor→target chain; UI tests cover the drawer, History entry, collapse, and role attribution.

## Out of scope

- Conversations as a primary nav door
- HP-120 binaries
- Telegram 4-door ops cutover

## Testing

- [ ] `cd web && npm test -- src/components/views/RunDetailPanel.test.tsx src/components/views/ConversationMessageRow.test.tsx src/components/views/RunBoardView.test.tsx src/components/views/ConversationsView.test.tsx`
- [ ] `pytest tests/test_conversations_service.py -q`
