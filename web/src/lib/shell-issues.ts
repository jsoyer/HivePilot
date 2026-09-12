/**
 * Header + Alerts-door issue count for the redesign shell (PR1).
 *
 * Matches mock A: failed runs + degraded/error plugins. Pending approvals
 * stay on the Approvals door — they are not "issues" in this chip.
 * The Alerts *list* is a follow-up; this helper only counts.
 */
export function countShellIssues(
  plugins: ReadonlyArray<{ status: string }>,
  runs: ReadonlyArray<{ status: string }>,
): number {
  const pluginIssues = plugins.filter(
    (plugin) => plugin.status === 'degraded' || plugin.status === 'error',
  ).length
  const failedRuns = runs.filter((run) => run.status === 'failed').length
  return pluginIssues + failedRuns
}
