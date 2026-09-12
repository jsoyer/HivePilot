## Summary

HP-88: Rebuild the Pollen shell to match redesign mock A (web UI only). Inbox is the landing. Sidebar is four doors (Inbox / Approvals / Runs / Alerts) plus a Plus tray (Rooms / Orchestrator / Spend / Memory / System). Everything else stays reachable via ⌘K. Header is search + one issues chip + overflow (theme / language / account). Plugin status pills and the decorative grid are gone. Dark tokens and PWA theme-color follow the mock (`#09090b` / `#111113` / `#38bdf8`). TokenGate copy says “HivePilot token”, not “read token”.

Owning issue: [HP-88](https://linear.app/js-workspace/issue/HP-88/pollen-redesign-pr1-shell-inboxapprovalsrunsalerts-plus)

Replay: `cd web && npm test -- src/components/Pollen.test.tsx src/components/nav/SidebarNav.test.tsx src/components/nav/nav-config.test.ts`

## Follow-ups (not this PR)

- Runs History board
- Alerts list surface (this PR ships the door + a stub + the header chip count)
- Inbox ACTION buttons / KPI bandeau
- Telegram topics

## Testing

- [x] `cd web && npm test -- src/components/Pollen.test.tsx src/components/nav/SidebarNav.test.tsx src/components/nav/nav-config.test.ts src/components/nav/IssuesChip.test.tsx src/components/nav/OverflowMenu.test.tsx src/components/views/InboxView.test.tsx src/components/views/AlertsView.test.tsx src/lib/shell-issues.test.ts src/components/TokenGate.test.tsx src/lib/i18n/fr.test.ts`
- [x] `cd web && npm test` — 923 passed
- [x] `cd web && npm run build` (static `theme-color` `#09090b`; CI `git diff --exit-code hivepilot/webui/static`)
