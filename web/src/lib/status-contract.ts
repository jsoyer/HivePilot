/**
 * Derived-status contract (HP-42, Cycle 1 · P1 "Live & Missions").
 *
 * The single, pure mapping from a raw `RunSummary.status` to a board *column*
 * and an *attention zone* ("where should the operator look?"). Display status
 * is DERIVED from the status fact, never stored — so there is one place this
 * mapping lives on the web side.
 *
 * This MIRRORS the Python source of truth `hivepilot/services/
 * status_contract.py` (read it before changing anything here). The mapping
 * table is identical; only the identifier casing follows each language's
 * convention (`waitingApproval` here vs `waiting_approval` in Python). Both
 * sides are pinned by their own tests (`status-contract.test.ts` /
 * `test_status_contract.py`), and the Python side additionally guards its
 * failure/success sets against `analytics_service` so nothing drifts.
 */

export type RunColumn = 'queued' | 'running' | 'waitingApproval' | 'failed' | 'done' | 'other'

/** Attention zones, most → least urgent. Mirrors `status_contract.Zone`. */
export type AttentionZone = 'needs_you' | 'in_review' | 'working' | 'queued' | 'ready' | 'other'

/** Pre-execution: RunStatus.NEW/PLANNED + the `pending` literal `create_run`
 * stores for a require-approval initial run. */
export const QUEUED_STATUSES = new Set(['new', 'planned', 'pending'])
/** A human decision is pending: RunStatus.APPROVAL/REVIEW + `awaiting_approval`. */
export const WAITING_APPROVAL_STATUSES = new Set(['approval', 'awaiting_approval', 'review'])
/** Terminal failures — the canonical `analytics_service._FAILED_STATUSES`. */
export const FAILED_STATUSES = new Set([
  'failed',
  'denied',
  'rate_limit',
  'auth_expired',
  'test_failure',
  'security_blocker',
])
/** Terminal successes — the canonical `analytics_service._SUCCEEDED_STATUSES`. */
export const DONE_STATUSES = new Set(['success', 'complete'])

function normalise(status: string): string {
  return status.trim().toLowerCase()
}

/**
 * Maps a raw status to its Kanban column — faithfully, never inventing a
 * status. Anything unrecognized (paused, cancelled, deferred, unknown) lands
 * in `other`, exactly the bucket `analytics_service.canonical_outcome()` falls
 * back to for these.
 */
export function runColumn(status: string): RunColumn {
  const s = normalise(status)
  if (QUEUED_STATUSES.has(s)) return 'queued'
  if (s === 'running') return 'running'
  if (WAITING_APPROVAL_STATUSES.has(s)) return 'waitingApproval'
  if (FAILED_STATUSES.has(s)) return 'failed'
  if (DONE_STATUSES.has(s)) return 'done'
  return 'other'
}

/**
 * Maps a raw status to its attention zone. Derived from the raw status (not the
 * column), so `approval`/`awaiting_approval` (a decision is needed → `needs_you`)
 * and `review` (under review → `in_review`) split even though they share the
 * `waitingApproval` column.
 */
export function attentionZone(status: string): AttentionZone {
  const s = normalise(status)
  if (FAILED_STATUSES.has(s)) return 'needs_you'
  if (s === 'approval' || s === 'awaiting_approval') return 'needs_you'
  if (s === 'review') return 'in_review'
  if (s === 'running') return 'working'
  if (QUEUED_STATUSES.has(s)) return 'queued'
  if (DONE_STATUSES.has(s)) return 'ready'
  return 'other'
}

/** True when a human should look now (a failure or a pending decision). */
export function needsAttention(status: string): boolean {
  return attentionZone(status) === 'needs_you'
}
