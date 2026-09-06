## Summary

HP-66: Agent Studio UI on the store-backed `/v1/roles` CRUD (HP-25).

- New System tab **Studio**: roster table + drawer editor.
- Reads for any token; New / Save / Delete only when `can('admin')`.
- Payload matches `RoleWrite` (name, title, profile, runner, model, inputs/outputs, can_block, order, prompt_text / prompt_file).
- Known roles reuse HP-20 `RoleAvatar`.

Does not add NL authoring (HP-24 Phase 3). Does not change the roles store or governance (`bypassPermissions` still fail-closed server-side).

Linear: [HP-66](https://linear.app/js-workspace/issue/HP-66/ui-agent-studio-front-crud-des-roles-backend-hp-25-livre). Parent HP-38.

## Testing

- [x] `cd web && npm test -- --run src/components/views/AgentStudioView.test.tsx src/components/Pollen.test.tsx` (29 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-BBCGRB4x.js`)

Replay: Pollen → Studio; admin token can create/edit/delete; read token sees the roster only.
