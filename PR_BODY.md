## Summary

HP-61: inline approval cards in Operate Chat plus persistable per-action auto-approve / auto-deny rules. No Disposition field. The existing `POST /v1/approvals/{id}` path stays the resolver.

- Chat: concierge `approve`/`deny` with `run_id` renders `ApprovalActionCard` (not a proposal-only card)
- `GET/PUT /v1/approval-rules` — admin writes; empty match fields are wildcards; more specific wins
- Policy hook after `require_approval` / pipeline checkpoint: matching `approve` skips the pause; `deny` records denied
- CORS allows `PUT`

Linear: [HP-61](https://linear.app/js-workspace/issue/HP-61/cartes-dapprobation-regles-par-action-inline-dans-le-chat).

Replay: send a concierge “approve run N” in Chat; or `PUT /v1/approval-rules` then run a `require_approval` task.

## Testing

- [x] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_hp61_approval_rules.py -q`
- [x] `cd web && npm test -- --run src/components/views/ChatView.test.tsx src/lib/pollen-api.test.ts`
