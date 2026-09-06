## Summary

HP-80: compact **Missions** rail beside the Espaces conversation thread.

- Right-hand rail of the last 12 runs (`fetchRuns(12)`), stacked under the thread below `lg`.
- Reuses HP-42 attention zones + HP-44 `StatusGlyph`, heartbeat vs started timestamps.
- Live refresh on existing SSE `run` events (no second stream hook).
- Classify-only: no dispatch, no model badge (`RunSummary` has none), never renders `detail`.

Linear: [HP-80](https://linear.app/js-workspace/issue/HP-80/rail-missions-dans-lespace-conversation-missions-cote-a-cote).

## Testing

- [x] `cd web && npm test -- --run src/components/espaces/MissionsRail.test.tsx src/components/views/EspacesView.test.tsx src/components/Pollen.test.tsx src/lib/i18n/fr.test.ts src/lib/i18n/en.test.ts`
- [x] `cd web && npm run build` (Node 26.5.0 → `index-BB4qE0OD.js`)

Replay: Pollen → Operate → Spaces; open a room; the Missions rail lists recent runs with status glyphs.
