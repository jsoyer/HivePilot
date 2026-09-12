## Summary

HP-89: Redesign the Pollen Runs view to match mock B (web UI only). Live work stays on a four-column Board (Queued · Running · Waiting · Failed). Completed runs move to a **History** tab — there is no Done kanban column. Cards are title / project / age; Failed uses a 2px crit left border and no glow. Empty columns stay equal-width rails with an em dash. New run CTA is unchanged. The HP-42 `runColumn` contract is untouched; `boardPlacement` is a presentation overlay (success/complete/cancelled → History; paused/deferred stay visible under Waiting).

Owning issue: [HP-89](https://linear.app/js-workspace/issue/HP-89/pollen-redesign-pr2-runs-board-history-done-out-of-kanban)

Replay: `cd web && npm test -- src/components/views/RunBoardView.test.tsx src/lib/i18n/fr.test.ts`

## Follow-ups (not this PR)

- Alerts first-class list / sidepanel (PR3) — the PR1 door stub stays
- Inbox ACTION buttons / KPI bandeau (PR4)
- Telegram topics (PR5)

## Testing

- [ ] `cd web && npm test -- src/components/views/RunBoardView.test.tsx src/lib/i18n/fr.test.ts`
- [ ] `cd web && npm test`
- [ ] `cd web && npm run build`
