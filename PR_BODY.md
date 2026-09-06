## Summary

HP-20: per-role procedural avatars on the Pollen Agents page.

- Pin `@bible-strong/avatar-react` + `@bible-strong/avatar-core` at `0.1.0`.
- One Strobi base rig (`web/src/assets/avatars/base.avatar.json`); each of the 8 roles overrides body colour only.
- `<RoleAvatar>` maps run state → animation (`idle` / `working` / `thinking` / `happy` / `sad` / `suspicious`).
- Wired on the Agents roster, attention band, and role drawer. Unmapped roles keep the hashed initial badge.
- AGPL-3.0-only renderer documented in `web/src/assets/avatars/ATTRIBUTION.md` (compatible with HivePilot GPL-3.0).

Rebased onto current `main` (includes HP-55 Knowledge panel + HP-53 mem0 retirement). Rebuilt committed `hivepilot/webui/static/`.

Does not author 8 unique Lab exports, and does not yet replace badges on Sweep graph nodes / activity feed / run cards.

Linear: [HP-20](https://linear.app/js-workspace/issue/HP-20/role-avatars-in-pollen-animated-bible-strongavatar-react).

## Testing

- [x] `cd web && npm test -- --run src/components/RoleAvatar.test.tsx src/components/views/AgentsView.test.tsx`
- [x] `cd web && npm run build` (Node 26.5.0)
- [ ] `pytest` — re-run after push; previous CI failure was against stale `main`
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: open Pollen → Agents; the eight first-class roles show coloured procedural avatars; custom roles stay initials.
