Owning issue: [HP-119](https://linear.app/js-workspace/issue/HP-119/r1-pollen-onglet-conversations-interactions-actortarget)

Replay: `cd web && npm test -- src/components/views/RunDetailPanel.test.tsx src/components/views/ConversationMessageRow.test.tsx src/components/views/RunBoardView.test.tsx src/components/views/ConversationsView.test.tsx`

## Summary

HP-119 — Conversations is the third **Runs** segment: `Board | History | Conversations`. Not a fifth sidebar door, not under Plus, not a run-detail tab.

Matches the Aphrodite mock: left list of runs that have speech, right actor→target fil, kit role colours (`ROLE_AVATAR_COLORS`), outputs collapsed until expanded, reply framed as a correction for the role’s next run.

## How to reach it

Pollen → **Runs** → **Conversations**.

## What changed

1. **Runs surface** — third pill on the existing Board/History control.
2. **Layout** — runs-with-speech list + thread for the selected run id.
3. **Fil** — `actor → target` + action + clock; role badges use HP-20 kit colours, not sky chrome.
4. **Outputs** — collapsed by default; Expand/Collapse on the turn.
5. **API** — `Message.target` from `interactions.target`.
6. **Nav** — Conversations removed from ⌘K / System lab so it cannot come back as a fifth destination.
7. **Tests + fixture** — `web/src/test/fixtures/interactions.ts`.

## Out of scope

- Conversations as a primary nav door
- HP-120 binaries
- Telegram 4-door ops cutover

## Testing

- [ ] UI tests (replay above)
- [ ] `pytest tests/test_conversations_service.py -q`
