## Summary

HP-91: Inbox complements for the Pollen redesign (web UI). ACTION / ROUTE / MULTI_ROUTE replies render a confirm card (`intent: ACTION · confirmation required`) with only ✅ Confirm / ❌ Cancel. ANSWER never shows that keyboard. Nothing runs until Confirm; Cancel and ANSWER dispatch nothing. Approve/deny and `action=run` reuse the existing Approvals / Runs APIs. Pipeline and route stay acknowledged-only (no fabricated pipeline dispatch).

Under the Inbox title, a collapsible **Snapshot** bandeau shows three KPIs (Spend 24h · Success · Pending) plus a compact “N issues → Alerts” strip that reuses the PR3 `buildAlertFeed` / `countAlertIssues` counts. Inbox is still the landing door — not a saturated Home, no SweepRadar.

Owning issue: [HP-91](https://linear.app/js-workspace/issue/HP-91/pollen-redesign-pr4-inbox-action-confirm-kpi-snapshot-bandeau)

Replay: `cd web && npm test -- src/lib/concierge-intent.test.ts src/components/views/ChatView.test.tsx src/components/views/InboxView.test.tsx src/lib/i18n/fr.test.ts`

## Follow-ups (not this PR)

- Telegram 4 topics (PR5)

## Testing

- [x] `cd web && npm test -- src/lib/concierge-intent.test.ts src/components/views/ChatView.test.tsx src/components/views/InboxView.test.tsx src/lib/i18n/fr.test.ts` — 22 passed
- [x] `cd web && npm test` — 952 passed
- [x] `cd web && npm run build` (refreshed `hivepilot/webui/static`; CI `git diff --exit-code hivepilot/webui/static`)
