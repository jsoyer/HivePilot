import {
  AlertTriangle,
  CheckSquare,
  Clock,
  Database,
  DollarSign,
  PlayCircle,
  Radar as RadarIcon,
  Zap,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { SectionHeader } from '@/components/dashboard/SectionHeader'
import { StatusGlyph } from '@/components/dashboard/StatusGlyph'
import { SweepRadar, type RadarAgent, type RadarAgentStatus } from '@/components/dashboard/SweepRadar'
import type { VizTone } from '@/components/dashboard/Sparkline'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  type Approval,
  fetchAnalyticsCost,
  fetchAnalyticsSummary,
  fetchApprovals,
  fetchEfficiency,
  fetchMemoryReality,
  fetchRuns,
  postApproval,
  type RunSummary,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { FAILED_STATUSES } from '@/lib/status-contract'
import type { AsyncState } from '@/lib/use-async-data'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'

/** Rolling window, not a calendar day — `days=1` to the API means "the
 * last 24h" (see `_resolve_window` in `analytics_service.py`), not
 * midnight-to-now. Labeled "last 24h" in the UI (`home.kpiSpendSub` /
 * `home.kpiRunsSub`) rather than "today" for that reason — honest about
 * what the window actually covers. */
const TODAY_DAYS = 1
/** Matches `MemoryQualityView`'s own default window for memory-quality signals —
 * a rolling 24h window is usually too narrow for search/evaluation volume
 * to be meaningful. */
const MEMORY_DAYS = 30
/** Modest live-ish poll cadence for runs/approvals/spend — reuses
 * `RunsView`'s `setInterval` + `refreshKey` pattern (see that file), at a
 * less aggressive cadence appropriate for a summary dashboard rather than
 * an actively-watched run list. */
const POLL_INTERVAL_MS = 5000
const MAX_ATTENTION_RUNS = 5
const MAX_FEED_ITEMS = 10

export interface HomeViewProps {
  /** Switches Pollen's active view (the same `setActiveView` the sidebar/
   * command palette drive) — every hero KPI card is a deep link into the
   * view that explains it. */
  onNavigate: (view: string) => void
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

/**
 * Bug fix (permanent clipping): a raw comma-grouped token count (e.g.
 * "623,393,633") is wider than the hero KPI card has room for in a 5-column
 * grid — it overflowed its `MetricReadout` and got sliced by the ancestor
 * `Card`'s `overflow-hidden` with no ellipsis, no wrap, just a hard cut
 * mid-digit. Above 1,000,000 this switches to compact notation ("623.4M",
 * matching the reference mockup's "1.8M" KPI style) — below that threshold
 * the exact grouped count is short enough to always fit, so it's kept
 * unchanged (existing behavior/tests for small values like "1,500").
 */
function formatTokens(n: number): string {
  const rounded = Math.round(n)
  if (Math.abs(rounded) >= 1_000_000) {
    return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(rounded)
  }
  return rounded.toLocaleString('en-US')
}

function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`
}

/** Elapsed time from `iso` to now, as a short "2d"/"3h"/"12m"/"45s" string —
 * mirrors `MemoryQualityView`'s `formatFreshness` but computed from "now" rather
 * than a duration already expressed in seconds. Unparseable/missing input
 * renders as "—", never a fabricated age. */
function formatAge(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const totalSeconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  if (days > 0) return `${days}d`
  if (hours > 0) return `${hours}h`
  if (minutes > 0) return `${minutes}m`
  return `${totalSeconds}s`
}

/** Canonical run failure statuses — the shared derived-status contract's
 * `FAILED_STATUSES` (HP-42), the single source of truth mirrored from
 * `hivepilot/services/status_contract.py` (itself pinned to
 * `analytics_service._FAILED_STATUSES`). */
const FAILURE_STATUSES = FAILED_STATUSES

/** `RunSummary.status` -> `SweepRadar`'s status dot color. Mirrors
 * `hivepilot/services/state_service.py`'s `RunStatus` values (`"running"`,
 * `"awaiting_approval"`) and `FAILURE_STATUSES` above — everything else
 * (terminal-success, cancelled, queued/planned/paused/review, ...) is
 * `'idle'`, never guessed into `'good'`/`'crit'`. */
function runRadarStatus(status: string): RadarAgentStatus {
  if (status === 'running') return 'good'
  if (status === 'awaiting_approval' || status === 'approval') return 'warn'
  if (FAILURE_STATUSES.has(status)) return 'crit'
  return 'idle'
}

const TONE_DOT: Record<VizTone, string> = {
  default: 'bg-muted-foreground/40',
  good: 'bg-[var(--color-good)]',
  warn: 'bg-[var(--color-warn)]',
  crit: 'bg-[var(--color-crit)]',
}

/** `ActivityEntry.status`/`kind` -> the same tone family the rest of
 * Pollen's viz primitives use, for the activity feed's status dot. */
function activityTone(status: string): VizTone {
  if (FAILURE_STATUSES.has(status)) return 'crit'
  if (status === 'running' || status === 'awaiting_approval' || status === 'approval' || status === 'pending') {
    return 'warn'
  }
  if (status === 'success' || status === 'complete') return 'good'
  return 'default'
}

/**
 * Wraps any hero KPI in a real `<button>` so the whole card is a deep link
 * into the view that explains it (click -> `onNavigate`), keyboard-operable
 * for free (native `<button>` semantics) without hand-rolling
 * `role="button"` + `onKeyDown` handling.
 */
function KpiButton({
  testId,
  ariaLabel,
  onClick,
  children,
}: {
  testId: string
  ariaLabel: string
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={ariaLabel}
      onClick={onClick}
      className="block w-full rounded-xl text-left transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
    >
      {children}
    </button>
  )
}

interface KpiCardProps<T> {
  testId: string
  icon: ReactNode
  label: string
  state: AsyncState<T>
  onNavigate: () => void
  render: (data: T) => { value: ReactNode; sub?: ReactNode; tone?: VizTone }
}

/**
 * Generic `MetricReadout`-backed hero KPI: renders loading/error(incl.
 * `ApiForbiddenError`)/success uniformly so each KPI below only has to
 * describe how to turn its own resolved data into a value/sub/tone triple.
 * A 403 never crashes the card — it renders an honest "—" plus a
 * "requires a higher-role token" sub-line instead (never a fabricated `0`).
 */
function KpiCard<T>({ testId, icon, label, state, onNavigate, render }: KpiCardProps<T>) {
  const t = useT()
  let value: ReactNode = '—'
  let sub: ReactNode
  let tone: VizTone = 'default'

  if (state.status === 'loading') {
    sub = t('common.loading')
  } else if (state.status === 'error') {
    sub = state.error instanceof ApiForbiddenError ? t('home.kpiRequiresRole') : describeApiError(state.error)
  } else {
    const rendered = render(state.data)
    value = rendered.value
    sub = rendered.sub
    tone = rendered.tone ?? 'default'
  }

  return (
    <KpiButton testId={testId} ariaLabel={label} onClick={onNavigate}>
      <MetricReadout icon={icon} label={label} value={value} sub={sub} tone={tone} />
    </KpiButton>
  )
}

interface AttentionApprovalRowProps {
  approval: Approval
  canApprove: boolean
  onDone: () => void
}

/**
 * A pending approval inside "Needs attention" — compact Approve/Deny
 * controls, same contract as `ApprovalsView`'s `RowActions` (deny requires
 * a non-empty reason, both actions disable while in flight). Never renders
 * `Approval.metadata` (untrusted free text) — only the typed project/task/
 * age fields.
 */
function AttentionApprovalRow({ approval, canApprove, onDone }: AttentionApprovalRowProps) {
  const t = useT()
  const [denyOpen, setDenyOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(approve: boolean) {
    setSubmitting(true)
    setError(null)
    try {
      await postApproval(approval.run_id, { approve, reason: approve ? undefined : reason.trim() })
      onDone()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('approvals.insufficientRoleApprove') : describeApiError(err))
      setSubmitting(false)
    }
  }

  return (
    <li
      data-testid={`home-attention-approval-${approval.run_id}`}
      className="flex flex-col gap-2 rounded-lg border border-border p-3 text-sm transition-colors hover:bg-muted/40"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{t('home.attentionApprovalBadge')}</Badge>
        <span className="metric-mono text-xs text-muted-foreground">#{approval.run_id}</span>
        <span className="font-medium">{approval.project}</span>
        <span className="text-muted-foreground">·</span>
        <span>{approval.task}</span>
        <span className="ml-auto text-xs text-muted-foreground">
          {t('home.ageAgo', { age: formatAge(approval.requested_at) })}
        </span>
      </div>
      {canApprove && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            disabled={submitting}
            onClick={() => {
              void submit(true)
            }}
            aria-label={t('approvals.approveAriaLabel', { id: approval.run_id })}
          >
            {t('approvals.approve')}
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={submitting}
            onClick={() => setDenyOpen((open) => !open)}
            aria-label={t('approvals.denyAriaLabel', { id: approval.run_id })}
          >
            {t('approvals.deny')}
          </Button>
          {submitting && (
            <span role="status" className="text-xs text-muted-foreground">
              {t('common.processing')}
            </span>
          )}
        </div>
      )}
      {denyOpen && (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t('approvals.reasonPlaceholder')}
            aria-label={t('approvals.denialReasonAriaLabel', { id: approval.run_id })}
            disabled={submitting}
          />
          <Button
            size="sm"
            variant="destructive"
            disabled={submitting || !reason.trim()}
            onClick={() => {
              void submit(false)
            }}
          >
            {t('approvals.confirmDeny')}
          </Button>
        </div>
      )}
      {error && (
        <div role="alert" className="text-xs text-destructive">
          {error}
        </div>
      )}
    </li>
  )
}

/** A recently-failed run inside "Needs attention" — read-only (no action;
 * `RunsView` already owns cancel/stop for `status === 'running'`). Never
 * renders `RunSummary.detail` (untrusted free text). */
function AttentionRunRow({ run }: { run: RunSummary }) {
  const t = useT()
  return (
    <li
      data-testid={`home-attention-run-${run.id}`}
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm transition-colors hover:bg-muted/40"
    >
      <StatusGlyph status={run.status} />
      <Badge variant="destructive">{t('home.attentionFailedRunBadge')}</Badge>
      <span className="metric-mono text-xs text-muted-foreground">#{run.id}</span>
      <span className="font-medium">{run.project}</span>
      <span className="text-muted-foreground">·</span>
      <span>{run.task}</span>
      <span className="ml-auto text-xs text-muted-foreground">{t('home.ageAgo', { age: formatAge(run.started_at) })}</span>
    </li>
  )
}

interface NeedsAttentionSectionProps {
  approvalsState: AsyncState<Approval[]>
  runsState: AsyncState<RunSummary[]>
  canApprove: boolean
  onDone: () => void
}

/**
 * "Needs attention" — pending approvals (oldest first) followed by recent
 * failed runs (most-recent-first, capped at `MAX_ATTENTION_RUNS`). Collapses
 * to a single "all clear" row when both lists are genuinely empty (never
 * disappears entirely — an operator should always see SOME signal here).
 * A 403 on BOTH underlying endpoints renders one graceful message instead
 * of two separate error cards; a 403 on only one still shows the other.
 */
function NeedsAttentionSection({ approvalsState, runsState, canApprove, onDone }: NeedsAttentionSectionProps) {
  const t = useT()
  const approvalsForbidden = approvalsState.status === 'error' && approvalsState.error instanceof ApiForbiddenError
  const runsForbidden = runsState.status === 'error' && runsState.error instanceof ApiForbiddenError

  if (approvalsForbidden && runsForbidden) {
    return (
      <p data-testid="home-attention-forbidden" className="text-sm text-muted-foreground">
        {t('home.needsAttentionForbidden')}
      </p>
    )
  }

  if (approvalsState.status === 'loading' || runsState.status === 'loading') {
    return (
      <div role="status" className="animate-pulse text-sm text-muted-foreground">
        {t('common.loading')}
      </div>
    )
  }

  const approvals =
    approvalsState.status === 'success'
      ? [...approvalsState.data].sort(
          (a, b) => new Date(a.requested_at).getTime() - new Date(b.requested_at).getTime(),
        )
      : []
  const failedRuns =
    runsState.status === 'success'
      ? runsState.data
          .filter((run) => FAILURE_STATUSES.has(run.status))
          .sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime())
          .slice(0, MAX_ATTENTION_RUNS)
      : []

  if (approvals.length === 0 && failedRuns.length === 0) {
    return (
      <p data-testid="home-all-clear" className="text-sm text-muted-foreground">
        {t('home.allClear')}
      </p>
    )
  }

  return (
    <ul className="flex flex-col gap-2">
      {approvals.map((approval) => (
        <AttentionApprovalRow key={approval.run_id} approval={approval} canApprove={canApprove} onDone={onDone} />
      ))}
      {failedRuns.map((run) => (
        <AttentionRunRow key={run.id} run={run} />
      ))}
    </ul>
  )
}

function LegendEntry({
  testId,
  colorVar,
  label,
  count,
}: {
  testId: string
  colorVar: string
  label: string
  count: number
}) {
  return (
    <span data-testid={testId} className="flex items-center gap-1.5">
      <span aria-hidden="true" className="inline-block size-2 rounded-full" style={{ backgroundColor: `var(${colorVar})` }} />
      {label} <span className="metric-mono">{count}</span>
    </span>
  )
}

/**
 * "The Sweep" — `SweepRadar` fed by REAL active/recent runs (`GET
 * /v1/runs`), status-mapped via `runRadarStatus`, with a counted legend
 * underneath. Honest empty state when there are no runs at all (never a
 * fabricated placeholder fleet).
 */
function SweepSection({ runsState }: { runsState: AsyncState<RunSummary[]> }) {
  const t = useT()

  if (runsState.status === 'loading') {
    return (
      <div role="status" className="animate-pulse text-sm text-muted-foreground">
        {t('common.loading')}
      </div>
    )
  }

  if (runsState.status === 'error') {
    if (runsState.error instanceof ApiForbiddenError) {
      return (
        <div
          data-testid="home-sweep-forbidden"
          className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
        >
          {t('common.requiresRunRankLead')} <span className="font-medium text-foreground">run-rank</span>{' '}
          {t('common.requiresRunRankTail')}
        </div>
      )
    }
    return (
      <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
        {describeApiError(runsState.error)}
      </div>
    )
  }

  const runs = runsState.data
  if (runs.length === 0) {
    return (
      <p data-testid="home-sweep-empty" className="text-sm text-muted-foreground">
        {t('home.sweepEmpty')}
      </p>
    )
  }

  const agents: RadarAgent[] = runs.map((run) => ({
    id: String(run.id),
    label: run.task,
    status: runRadarStatus(run.status),
  }))
  const counts = { good: 0, warn: 0, crit: 0, idle: 0 }
  for (const agent of agents) counts[agent.status] += 1

  return (
    <div data-testid="home-sweep" className="flex flex-col items-center gap-3">
      <SweepRadar agents={agents} ariaLabel={t('home.sweepTitle')} className="max-w-[220px]" />
      <div className="grid w-full grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-4">
        <LegendEntry
          testId="home-sweep-legend-running"
          colorVar="--color-good"
          label={t('home.sweepLegendRunning')}
          count={counts.good}
        />
        <LegendEntry
          testId="home-sweep-legend-waiting"
          colorVar="--color-warn"
          label={t('home.sweepLegendWaiting')}
          count={counts.warn}
        />
        <LegendEntry
          testId="home-sweep-legend-failed"
          colorVar="--color-crit"
          label={t('home.sweepLegendFailed')}
          count={counts.crit}
        />
        <LegendEntry
          testId="home-sweep-legend-idle"
          colorVar="--color-idle"
          label={t('home.sweepLegendIdle')}
          count={counts.idle}
        />
      </div>
    </div>
  )
}

interface ActivityEntry {
  key: string
  ts: string
  kind: 'run' | 'approval'
  status: string
  project: string
  task: string
}

/** Merges runs + approvals into one newest-first feed, capped at
 * `MAX_FEED_ITEMS`. Pure function (no fetching) so it's trivially testable
 * against fixture data. */
function buildActivityFeed(runs: RunSummary[], approvals: Approval[]): ActivityEntry[] {
  const runEntries: ActivityEntry[] = runs.map((run) => ({
    key: `run-${run.id}`,
    ts: run.started_at,
    kind: 'run',
    status: run.status,
    project: run.project,
    task: run.task,
  }))
  const approvalEntries: ActivityEntry[] = approvals.map((approval) => ({
    key: `approval-${approval.run_id}`,
    ts: approval.requested_at,
    kind: 'approval',
    status: approval.status,
    project: approval.project,
    task: approval.task,
  }))
  return [...runEntries, ...approvalEntries]
    .sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
    .slice(0, MAX_FEED_ITEMS)
}

interface ActivityFeedSectionProps {
  runsState: AsyncState<RunSummary[]>
  approvalsState: AsyncState<Approval[]>
}

/**
 * Recent runs + approvals, most-recent-first. Never renders
 * `RunSummary.detail`/`Approval.metadata` (untrusted free text) — only the
 * typed project/task/status/timestamp fields, all through plain JSX text
 * interpolation (React's auto-escaping), never `dangerouslySetInnerHTML`.
 */
function ActivityFeedSection({ runsState, approvalsState }: ActivityFeedSectionProps) {
  const t = useT()
  const runsForbidden = runsState.status === 'error' && runsState.error instanceof ApiForbiddenError
  const approvalsForbidden = approvalsState.status === 'error' && approvalsState.error instanceof ApiForbiddenError

  if (runsForbidden && approvalsForbidden) {
    return (
      <p data-testid="home-activity-forbidden" className="text-sm text-muted-foreground">
        {t('home.needsAttentionForbidden')}
      </p>
    )
  }

  if (runsState.status === 'loading' || approvalsState.status === 'loading') {
    return (
      <div role="status" className="animate-pulse text-sm text-muted-foreground">
        {t('common.loading')}
      </div>
    )
  }

  const runs = runsState.status === 'success' ? runsState.data : []
  const approvals = approvalsState.status === 'success' ? approvalsState.data : []
  const entries = buildActivityFeed(runs, approvals)

  if (entries.length === 0) {
    return (
      <p data-testid="home-activity-empty" className="text-sm text-muted-foreground">
        {t('home.activityFeedEmpty')}
      </p>
    )
  }

  return (
    <ul data-testid="home-activity-feed" className="flex flex-col gap-2">
      {entries.map((entry) => (
        <li
          key={entry.key}
          data-testid={`home-activity-${entry.key}`}
          className="flex flex-wrap items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors hover:bg-muted/40"
        >
          <span aria-hidden="true" className={cn('inline-block size-2 shrink-0 rounded-full', TONE_DOT[activityTone(entry.status)])} />
          <span className="metric-mono text-xs text-muted-foreground">{formatAge(entry.ts)}</span>
          <Badge variant="outline">{entry.kind === 'run' ? t('home.activityRunLabel') : t('home.activityApprovalLabel')}</Badge>
          <span className="metric-mono text-xs text-muted-foreground">{entry.key}</span>
          <span className="font-medium">{entry.project}</span>
          <span className="text-muted-foreground">·</span>
          <span>{entry.task}</span>
          <span className="ml-auto text-xs text-muted-foreground">{entry.status}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * Pollen's Home view — the default landing view (consumes
 * FE-1's viz primitives + the Pollen data endpoints sprint's
 * `/v1/models`, `/v1/efficiency`, `/v1/memory/growth`, extended
 * `/v1/analytics/cost`, plus the already-shipped `/v1/analytics/summary`,
 * `/v1/memory/reality`, `/v1/approvals`, `/v1/runs`).
 *
 * Five clickable hero KPIs deep-link into the view that explains them
 * (`onNavigate`, the same `setActiveView` the sidebar/palette drive), a
 * prioritized "Needs attention" list, "The Sweep" (a live-ish agent radar
 * fed by real run data), and a merged runs+approvals activity feed.
 *
 * **Honesty over completeness** (mirrors `MemoryQualityView`'s convention): every
 * KPI/section falls back to an explicit "not available"/"no data"/"—"
 * state instead of a fabricated number when its underlying signal is
 * genuinely absent (no daily-budget field exists anywhere in the API, so
 * the spend KPI never shows a fake burn gauge; the tokens-saved KPI shows
 * "not available" rather than `0` when neither headroom nor rtk has
 * anything recorded).
 *
 * **Live-ish**: runs/approvals/spend poll on `POLL_INTERVAL_MS` (reuses
 * `RunsView`'s `setInterval` + `refreshKey` pattern) — efficiency/memory-
 * health/today's-summary are cheap enough to fetch once per mount without
 * polling.
 */
export function HomeView({ onNavigate }: HomeViewProps) {
  const t = useT()
  const { can } = useRole()
  const canApprove = can('approve')
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [])

  const cost = useAsyncData(() => fetchAnalyticsCost(TODAY_DAYS), [refreshKey])
  const approvalsState = useAsyncData(() => fetchApprovals(), [refreshKey])
  const runsState = useAsyncData(() => fetchRuns(), [refreshKey])
  const summary = useAsyncData(() => fetchAnalyticsSummary(TODAY_DAYS), [])
  const efficiency = useAsyncData(() => fetchEfficiency(MEMORY_DAYS), [])
  const memory = useAsyncData(() => fetchMemoryReality(MEMORY_DAYS), [])

  function handleAttentionDone() {
    setRefreshKey((key) => key + 1)
  }

  // Stale-while-revalidate (see `useAsyncData`): none of these three ever
  // flip the page back to a loading skeleton on poll — `isRefreshing` is
  // just a subtle in-place signal that a background refetch is in flight,
  // surfaced once here rather than three times (one per polled card).
  const isRefreshing =
    (cost.status === 'success' && cost.isRefreshing === true) ||
    (approvalsState.status === 'success' && approvalsState.isRefreshing === true) ||
    (runsState.status === 'success' && runsState.isRefreshing === true)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-baseline gap-2">
        <div>
          <h2 className="text-lg font-semibold">{t('nav.home')}</h2>
          <p className="text-sm text-muted-foreground">{t('home.subtitle')}</p>
        </div>
        {isRefreshing && (
          <span
            data-testid="home-refreshing-indicator"
            role="status"
            className="metric-mono ml-auto flex items-center gap-1.5 text-[10px] tracking-wide text-muted-foreground uppercase"
          >
            <span
              aria-hidden="true"
              className="size-1.5 rounded-full bg-[var(--color-good)] shadow-[0_0_6px_var(--color-good)] motion-safe:animate-pulse"
            />
            {t('home.refreshingLabel')}
          </span>
        )}
      </div>

      <SectionHeader index="01" title={t('home.kpiSectionTitle')} className="-mb-1" />
      {/* Bug fix (KPI row uniformity): `min-w-0` on every grid item stops a
       * long/overflowing value from ever growing its OWN card wider than
       * its equal-`fr` grid track — without it, an overflowing child (like
       * the old raw token count) could make one card visually wider/
       * ragged than its siblings even though the grid math is the same. */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5 *:min-w-0">
        <KpiCard
          testId="home-kpi-spend"
          icon={<DollarSign className="size-4" />}
          label={t('home.kpiSpendToday')}
          state={cost}
          onNavigate={() => onNavigate('cost')}
          render={(data) => ({ value: formatCost(data.overall.cost_usd), sub: t('home.kpiSpendSub') })}
        />

        <KpiCard
          testId="home-kpi-tokens"
          icon={<Zap className="size-4" />}
          label={t('home.kpiTokensSaved')}
          state={efficiency}
          onNavigate={() => onNavigate('analytics')}
          render={(data) => {
            const nothingRecorded = data.headroom.total_compressions === 0 && data.rtk === null
            if (nothingRecorded) {
              return { value: t('home.notAvailable'), sub: t('home.kpiTokensSavedSub') }
            }
            const total = data.headroom.est_tokens_saved + (data.rtk?.tokens_saved ?? 0)
            return { value: formatTokens(total), sub: t('home.kpiTokensSavedSub'), tone: 'good' as VizTone }
          }}
        />

        <KpiCard
          testId="home-kpi-runs"
          icon={<PlayCircle className="size-4" />}
          label={t('home.kpiRunsSuccess')}
          state={summary}
          onNavigate={() => onNavigate('runs')}
          render={(data) => {
            if (data.success_rate === null) {
              return { value: data.total, sub: t('home.kpiRunsSub') }
            }
            const tone: VizTone = data.success_rate >= 0.8 ? 'good' : data.success_rate >= 0.5 ? 'warn' : 'crit'
            return { value: data.total, sub: `${pct(data.success_rate)} · ${t('home.kpiRunsSub')}`, tone }
          }}
        />

        {/* Bug fix (KPI row uniformity): Memory health used to be a bespoke
         * `Card` with a centered `Gauge` and no icon chip — the one card in
         * the row that didn't match the icon + label + value + sub
         * structure every other `KpiCard` uses. Same primitive now, same
         * honest-empty handling (an unavailable/zero-searches reading is a
         * real "—" + "No data" sub-line, never a fabricated percentage). */}
        <KpiCard
          testId="home-kpi-memory"
          icon={<Database className="size-4" />}
          label={t('home.kpiMemoryHealth')}
          state={memory}
          onNavigate={() => onNavigate('memory')}
          render={(data) => {
            if (data.total_searches === 0) {
              return {
                value: <span data-testid="home-kpi-memory-nodata">{'—'}</span>,
                sub: t('home.noData'),
              }
            }
            const tone: VizTone =
              data.search_success_rate >= 0.8 ? 'good' : data.search_success_rate >= 0.5 ? 'warn' : 'crit'
            return { value: pct(data.search_success_rate), sub: t('home.kpiMemorySub'), tone }
          }}
        />

        <KpiCard
          testId="home-kpi-approvals"
          icon={<CheckSquare className="size-4" />}
          label={t('home.kpiPendingApprovals')}
          state={approvalsState}
          onNavigate={() => onNavigate('approvals')}
          render={(data) => ({
            value: data.length,
            sub: t('home.kpiApprovalsSub'),
            tone: data.length > 0 ? ('crit' as VizTone) : ('good' as VizTone),
          })}
        />
      </div>

      <Card>
        <CardHeader>
          <SectionHeader index="02" title={t('home.needsAttentionTitle')} />
          <CardDescription className="flex items-center gap-1.5">
            <AlertTriangle className="size-3.5 shrink-0" aria-hidden="true" />
            {t('home.needsAttentionDescription')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <NeedsAttentionSection
            approvalsState={approvalsState}
            runsState={runsState}
            canApprove={canApprove}
            onDone={handleAttentionDone}
          />
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <SectionHeader index="03" title={t('home.sweepTitle')} />
            <CardDescription className="flex items-center gap-1.5">
              <RadarIcon className="size-3.5 shrink-0" aria-hidden="true" />
              {t('home.sweepDescription')}
            </CardDescription>
          </CardHeader>
          <CardContent className="bg-grid-dot rounded-lg">
            <SweepSection runsState={runsState} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <SectionHeader index="04" title={t('home.activityFeedTitle')} />
            <CardDescription className="flex items-center gap-1.5">
              <Clock className="size-3.5 shrink-0" aria-hidden="true" />
              {t('home.activityFeedDescription')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ActivityFeedSection runsState={runsState} approvalsState={approvalsState} />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
