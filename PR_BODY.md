## Summary

HP-22: Pollen Chat tab that talks to the existing concierge brain (`POST /v1/concierge` → `concierge_service.route()`).

- New Operate tab **Chat** (Grok-style bubbles + composer).
- `route` / `action` / `multi_route` render as proposal cards with HP-20 `RoleAvatar`. Classify-only: does not dispatch runs.
- Same fail-closed semantics as Telegram. Independent of HP-18 (OSS runner).

Rebased onto current `main` (HP-20 avatars + HP-55/53 + Espaces/Orchestrator/MCP). Additive merge: Chat leads Operate, Spaces and Orchestrator stay.

Does not execute proposals. Does not gate on `chatops_concierge_enabled`. Does not touch HP-18.

Linear: [HP-22](https://linear.app/js-workspace/issue/HP-22/pollen-interactive-agent-chat-grok-bot-style-via-the-concierge).

## Testing

- [x] `cd web && npm test -- --run src/components/views/ChatView.test.tsx src/components/Pollen.test.tsx` (28 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-ChuVuSe5.js`)
- [x] `pytest tests/test_concierge_endpoint.py`

Replay: Pollen → Chat; send a message; answers render as bubbles, routes as proposal cards.
