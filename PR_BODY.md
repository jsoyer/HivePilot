## Summary

HP-90: Replace the PR1 Alerts door stub with a first-class worst-first list (web UI only). Failed runs sit at the top, then degraded plugins (installed-but-disabled, not on PATH, denied, toggled-off), then a Classifier row when the agent-surface probe has a real reading (`ok` or `unreachable`). The header **N issues** chip and the Alerts door badge count the same feed — a healthy Classifier is visible but does not increment the count. Run `detail` is never rendered. The System/Health dump stays under Plus → System.

Owning issue: [HP-90](https://linear.app/js-workspace/issue/HP-90/pollen-redesign-pr3-alerts-first-class-worst-first-list)

Replay: `cd web && npm test -- src/lib/alert-feed.test.ts src/lib/shell-issues.test.ts src/components/views/AlertsView.test.tsx src/lib/i18n/fr.test.ts`

## Follow-ups (not this PR)

- Inbox ACTION buttons / KPI bandeau (PR4)
- Telegram 4 topics (PR5)

## Testing

- [ ] `cd web && npm test -- src/lib/alert-feed.test.ts src/lib/shell-issues.test.ts src/components/views/AlertsView.test.tsx src/lib/i18n/fr.test.ts`
- [ ] `cd web && npm test`
- [ ] `cd web && npm run build` (refreshes `hivepilot/webui/static`; CI `git diff --exit-code hivepilot/webui/static`)
