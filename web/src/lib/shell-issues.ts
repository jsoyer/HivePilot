/**
 * Header + Alerts-door issue count for the redesign shell.
 *
 * The count is the same feed the Alerts page renders — Failed runs,
 * Degraded plugins, and a down Classifier. Pending approvals stay on the
 * Approvals door. A healthy Classifier row is visible on Alerts but is not
 * an "issue".
 */

import { buildAlertFeed, countAlertIssues, type AlertFeedInput } from './alert-feed'

export function countShellIssues(
  plugins: NonNullable<AlertFeedInput['plugins']>,
  runs: NonNullable<AlertFeedInput['runs']>,
  extras: Omit<AlertFeedInput, 'plugins' | 'runs'> = {},
): number {
  return countAlertIssues(buildAlertFeed({ plugins, runs, ...extras }))
}
