import { Plus } from 'lucide-react'
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { StatusGlyph } from '@/components/dashboard/StatusGlyph'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { formatAge, formatClock, formatElapsed, formatTimestamp } from '@/lib/format-time'
import { useT, type TranslationKey } from '@/lib/i18n'
import { cancelRun, fetchRuns, type RunSummary } from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import {
  type AttentionZone,
  attentionZone,
  DONE_STATUSES,
  FAILED_STATUSES,
  type RunColumn,
  runColumn,
} from '@/lib/status-contract'
import { useAsyncData } from '@/lib/use-async-data'
import { useEventStream } from '@/lib/use-event-stream'
import { usePersistedState } from '@/lib/use-persisted-state'
import { cn } from '@/lib/utils'
import { NewRunDrawer } from './NewRunDrawer'
import { RunDetailPanel } from './RunDetailPanel'

/** Poll cadence for `GET /v1/runs` — status transitions (running ->
 * success/failed/pending) show up without a manual refresh. Must stay
 * `<= 3000` per the sprint's acceptance criteria. */
const POLL_INTERVAL_MS = 3000

// How many runs the board requests. 50 used to be hard-coded with no way to
// ask for fewer, so an operator watching one pipeline had to read 50 cards.
// The API bounds the value to 1-500; these are the steps offered in the UI.
const RUN_LIMIT_OPTIONS = [10, 25, 50, 100, 200] as const
const DEFAULT_RUN_LIMIT = 50

// Status -> column/zone classification lives in the shared derived-status
// contract (`@/lib/status-contract`, HP-42), the single source of truth
// mirrored from `hivepilot/services/status_contract.py`. Re-exported here so
// existing importers of `RunBoardView` keep resolving `runColumn`/`RunColumn`.
export { type RunColumn, runColumn }

const COLUMN_ORDER: RunColumn[] = ['queued', 'running', 'waitingApproval', 'failed', 'done']

// Attention zones (HP-42/HP-43): the "where should I look?" lens over the board,
// most → least urgent. A representative status per zone drives the zone chip's
// glyph so it matches the cards' glyphs exactly.
const ATTENTION_ZONE_ORDER: AttentionZone[] = ['needs_you', 'in_review', 'working', 'queued', 'ready']

const ZONE_LABEL_KEY: Record<AttentionZone, TranslationKey> = {
  needs_you: 'board.zoneNeedsYou',
  in_review: 'board.zoneInReview',
  working: 'board.zoneWorking',
  queued: 'board.zoneQueued',
  ready: 'board.zoneReady',
  other: 'board.zoneOther',
}

const ZONE_SAMPLE_STATUS: Record<AttentionZone, string> = {
  needs_you: 'failed',
  in_review: 'review',
  working: 'running',
  queued: 'new',
  ready: 'success',
  other: 'cancelled',
}

const COLUMN_LABEL_KEY: Record<RunColumn, TranslationKey> = {
  queued: 'board.colQueued',
  running: 'board.colRunning',
  waitingApproval: 'board.colWaitingApproval',
  failed: 'board.colFailed',
  done: 'board.colDone',
  other: 'board.colOther',
}

/** Severity stripe — only on the two non-nominal columns (a human needs to
 * look: something failed, or something is blocked on a decision). Neither
 * `queued`/`running`/`done` gets one — all three are nominal states, not
 * something an operator needs to be visually flagged toward. */
const STRIPE_CLASS: Partial<Record<RunColumn, string>> = {
  failed: 'border-l-4 border-l-[var(--color-crit)]',
  waitingApproval: 'border-l-4 border-l-[var(--color-warn)]',
}

/**
 * Why a run is not nominal, in words, keyed off the ONLY real signal the
 * list endpoint carries: the canonical status.
 *
 * `RunSummary.detail` is untrusted, unredacted free text and is never
 * rendered anywhere in this app, so it cannot be the failure reason. The
 * status string, however, IS the classification the pipeline itself
 * assigned (`test_failure`, `security_blocker`, `rate_limit`, ...), which is
 * exactly the "why" an operator scanning the board needs. A status with no
 * entry here gets NO reason line — never a guessed one.
 *
 * Deliberately NOT shown on a card: cost. `GET /v1/runs` has no cost field
 * (only the per-run drill-down aggregates per-step cost), and fetching a
 * detail per card on every poll tick is exactly the N-requests-per-tick
 * pattern this view has always refused. A cost figure on a card would have
 * to be invented.
 */
const REASON_KEY: Record<string, TranslationKey> = {
  failed: 'board.reasonFailed',
  denied: 'board.reasonDenied',
  rate_limit: 'board.reasonRateLimit',
  auth_expired: 'board.reasonAuthExpired',
  test_failure: 'board.reasonTestFailure',
  security_blocker: 'board.reasonSecurityBlocker',
  cancelled: 'board.reasonCancelled',
  paused: 'board.reasonPaused',
  deferred: 'board.reasonDeferred',
}

function statusVariant(status: string): 'default' | 'secondary' | 'destructive' {
  const normalised = status.trim().toLowerCase()
  if (DONE_STATUSES.has(normalised)) return 'default'
  if (FAILED_STATUSES.has(normalised) || normalised === 'cancelled') return 'destructive'
  return 'secondary'
}

interface StopButtonProps {
  run: RunSummary
  onStopped: () => void
}

/**
 * Stop control for a single `status === 'running'` card (`POST /v1/runs/
 * {run_id}/cancel`) -- only rendered by the parent when `useRole().can('run')`
 * (defense-in-depth; the server enforces the same `run` role regardless of
 * what the client shows, see `cancel_run` in `api_service.py`). Requires
 * confirmation before sending the request. Cancellation is cooperative and
 * best-effort: the run resolves to `cancelled` at its NEXT step boundary, not
 * immediately -- this component doesn't wait for that, it relies on
 * `RunBoardView`'s existing poll loop (and an immediate `onStopped` refresh)
 * to surface the eventual status transition. A `409` (the run already
 * reached a terminal status between this card rendering and the click -- a
 * race with the poll loop, not a bug) surfaces as an inline error, never a
 * crash.
 *
 * The button's own `onClick` calls `stopPropagation` -- it lives inside a
 * clickable `RunCard` (click -> opens the run detail panel), and Stop must
 * never also open the detail panel out from under an in-flight cancel.
 */
function StopButton({ run, onStopped }: StopButtonProps) {
  const t = useT()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleStop() {
    if (!window.confirm(t('runs.stopConfirm', { id: run.id, task: run.task, project: run.project }))) return
    setSubmitting(true)
    setError(null)
    try {
      await cancelRun(run.id)
      onStopped()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('runs.insufficientRoleStop') : describeApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <Button
        size="sm"
        variant="destructive"
        disabled={submitting}
        onClick={(event) => {
          event.stopPropagation()
          void handleStop()
        }}
        aria-label={t('runs.stopAriaLabel', { id: run.id })}
      >
        {submitting ? t('common.stopping') : t('runs.stopButton')}
      </Button>
      {error && (
        <div role="alert" className="text-sm text-destructive">
          {error}
        </div>
      )}
    </div>
  )
}

export type BoardDensity = 'comfortable' | 'compact'

interface RunCardProps {
  run: RunSummary
  column: RunColumn
  density: BoardDensity
  canRun: boolean
  onOpenDetail: (runId: number) => void
  onStopped: () => void
}

/**
 * One Kanban card.
 *
 * Visual hierarchy, in priority order for someone scanning the board:
 *  1. a severity stripe on the two columns that need a human;
 *  2. the status chip, in semantic colour;
 *  3. the project (the thing an operator recognises), at full weight;
 *  4. everything else — run id, task, when it started, how long it took —
 *     in muted mono, subordinate.
 *
 * The "when" was the biggest omission in the previous card: it showed only
 * "ran for 8s" with no clue WHEN. Every card now carries a real local
 * timestamp (`formatClock`, full stamp on hover via `title`) next to the
 * duration.
 *
 * Never renders `RunSummary.detail` (untrusted free text) — only typed,
 * structural fields, plus a translated reason derived from the canonical
 * status (see `REASON_KEY`).
 *
 * The whole card is clickable (opens `RunDetailPanel`); keyboard-operable
 * via `role="button"`/`tabIndex`/Enter-or-Space (this is a `div`, not a
 * native `<button>`, because it also hosts a real nested `<button>` — the
 * Stop control — which native button-in-button nesting disallows).
 */
function RunCard({ run, column, density, canRun, onOpenDetail, onStopped }: RunCardProps) {
  const t = useT()
  const compact = density === 'compact'
  const reasonKey = REASON_KEY[run.status.trim().toLowerCase()]
  const duration = run.finished_at ? formatElapsed(run.started_at, run.finished_at) : formatAge(run.started_at)

  function open() {
    onOpenDetail(run.id)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      open()
    }
  }

  return (
    <Card
      data-testid={`run-board-card-${run.id}`}
      size="sm"
      role="button"
      tabIndex={0}
      aria-label={t('board.cardAriaLabel', { id: run.id, task: run.task, project: run.project })}
      onClick={open}
      onKeyDown={handleKeyDown}
      className={cn(
        'cursor-pointer gap-1.5 transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
        compact ? 'p-2' : 'p-3',
        STRIPE_CLASS[column],
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <StatusGlyph status={run.status} />
          <span className={cn('truncate font-medium', compact && 'text-xs')}>{run.project}</span>
        </div>
        <Badge variant={statusVariant(run.status)} className="shrink-0">
          {run.status}
        </Badge>
      </div>

      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="truncate text-muted-foreground">{run.task}</span>
        <span className="metric-mono shrink-0 text-muted-foreground">#{run.id}</span>
      </div>

      {!compact && (
        <div className="metric-mono flex items-baseline justify-between gap-2 text-xs text-muted-foreground">
          <span title={formatTimestamp(run.started_at)}>{formatClock(run.started_at)}</span>
          <span>
            {run.finished_at
              ? t('board.duration', { duration })
              : t('board.startedAgo', { age: duration })}
          </span>
        </div>
      )}

      {!compact && reasonKey && (
        <p
          data-testid={`run-board-reason-${run.id}`}
          className={cn(
            'text-xs',
            column === 'failed' ? 'text-[var(--color-crit)]' : 'text-muted-foreground',
          )}
        >
          {t(reasonKey)}
        </p>
      )}

      {canRun && run.status === 'running' && <StopButton run={run} onStopped={onStopped} />}
    </Card>
  )
}

interface RunColumnSectionProps {
  column: RunColumn
  runs: RunSummary[]
  density: BoardDensity
  canRun: boolean
  onOpenDetail: (runId: number) => void
  onStopped: () => void
}

/**
 * One board column.
 *
 * An EMPTY column collapses to a narrow rail instead of claiming the same
 * width as a column with forty cards in it — the previous board gave three
 * empty columns equal width and repeated "Nothing here." in each of them.
 * The count badge already says the column is empty; the body just holds an
 * em-dash so the rail still reads as a column and not as a rendering
 * failure.
 */
function RunColumnSection({
  column,
  runs,
  density,
  canRun,
  onOpenDetail,
  onStopped,
}: RunColumnSectionProps) {
  const t = useT()
  const empty = runs.length === 0

  return (
    <div
      data-testid={`run-board-column-${column}`}
      data-empty={empty ? 'true' : 'false'}
      className={cn(
        'flex flex-col gap-2 sm:shrink-0',
        empty ? 'sm:w-28' : 'sm:w-72',
        empty && 'opacity-70',
      )}
    >
      <div className="flex items-center justify-between gap-2 px-1">
        <h3 className={cn('truncate text-sm font-semibold', empty && 'text-muted-foreground')}>
          {t(COLUMN_LABEL_KEY[column])}
        </h3>
        <Badge variant="outline" data-testid={`run-board-count-${column}`} className="metric-mono shrink-0">
          {runs.length}
        </Badge>
      </div>
      <div
        className={cn(
          'flex flex-col gap-2 rounded-lg bg-muted/30 p-2',
          empty ? 'min-h-10 items-center justify-center' : 'min-h-16',
        )}
      >
        {empty ? (
          <span aria-hidden="true" className="metric-mono text-xs text-muted-foreground">
            —
          </span>
        ) : (
          runs.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              column={column}
              density={density}
              canRun={canRun}
              onOpenDetail={onOpenDetail}
              onStopped={onStopped}
            />
          ))
        )}
      </div>
    </div>
  )
}

interface RunBoardProps {
  runs: RunSummary[]
  density: BoardDensity
  canRun: boolean
  onOpenDetail: (runId: number) => void
  onStopped: () => void
}

/**
 * Groups `runs` by `runColumn` and renders one section per column. The
 * `'other'` column only appears when it actually has a run in it -- the
 * five canonical columns (`COLUMN_ORDER`) always render, even empty, so an
 * operator always sees the full board shape; `'other'` is the true "if a
 * status doesn't fit" edge case and stays out of the way otherwise.
 * Mobile: columns stack vertically; `sm:` and up: a horizontally-scrolling
 * row -- a Kanban board's natural responsive shape.
 *
 * The scroll region owns its own overflow (`kanban-scroll`, see
 * `index.css`) so the page body never scrolls sideways; `tabIndex={0}` +
 * `role="region"` + `aria-label` make it keyboard-scrollable rather than
 * mouse-drag-only; `min-w-0` keeps the row shrinkable so `overflow-x-auto`
 * can never be defeated by an ancestor flex context.
 */
function RunBoard({ runs, density, canRun, onOpenDetail, onStopped }: RunBoardProps) {
  const t = useT()
  const grouped: Record<RunColumn, RunSummary[]> = {
    queued: [],
    running: [],
    waitingApproval: [],
    failed: [],
    done: [],
    other: [],
  }
  for (const run of runs) grouped[runColumn(run.status)].push(run)

  const columns: RunColumn[] = grouped.other.length > 0 ? [...COLUMN_ORDER, 'other'] : COLUMN_ORDER

  return (
    <div
      data-testid="run-board-kanban-scroll"
      role="region"
      aria-label={t('board.kanbanScrollLabel')}
      tabIndex={0}
      className="kanban-scroll flex min-w-0 flex-col gap-4 sm:flex-row sm:items-start sm:overflow-x-auto sm:pb-2"
    >
      {columns.map((column) => (
        <RunColumnSection
          key={column}
          column={column}
          runs={grouped[column]}
          density={density}
          canRun={canRun}
          onOpenDetail={onOpenDetail}
          onStopped={onStopped}
        />
      ))}
    </div>
  )
}

const ALL = '__all__'
const NO_RUNS: RunSummary[] = []

interface AttentionSummaryProps {
  runs: RunSummary[]
  active: AttentionZone | null
  onSelect: (zone: AttentionZone | null) => void
}

/**
 * Attention-first lens over the board (HP-43): a row of zone chips — glyph +
 * label + count — ordered most → least urgent, driven by the shared
 * derived-status contract (HP-42). Clicking a zone filters the board to it (and
 * clicking it again, or "All", clears). Counts are over ALL runs (not the
 * filtered set) so the operator can always see and switch the full
 * distribution. A chip only appears when its zone is non-empty.
 */
function AttentionSummary({ runs, active, onSelect }: AttentionSummaryProps) {
  const t = useT()
  const counts = useMemo(() => {
    const c: Record<AttentionZone, number> = {
      needs_you: 0,
      in_review: 0,
      working: 0,
      queued: 0,
      ready: 0,
      other: 0,
    }
    for (const run of runs) c[attentionZone(run.status)] += 1
    return c
  }, [runs])

  const zones = [...ATTENTION_ZONE_ORDER, 'other' as const].filter((zone) => counts[zone] > 0)
  if (zones.length === 0) return null

  return (
    <div
      data-testid="board-attention-summary"
      role="group"
      aria-label={t('board.attentionFilterLabel')}
      className="flex flex-wrap items-center gap-2"
    >
      <span className="eyebrow">{t('board.attentionTitle')}</span>
      <button
        type="button"
        data-testid="board-attention-all"
        aria-pressed={active === null}
        onClick={() => onSelect(null)}
        className={cn(
          'rounded-full border px-2.5 py-1 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
          active === null
            ? 'border-primary bg-primary/10 text-foreground'
            : 'border-border text-muted-foreground hover:bg-muted',
        )}
      >
        {t('board.allZones')}
      </button>
      {zones.map((zone) => (
        <button
          key={zone}
          type="button"
          data-testid={`board-attention-zone-${zone}`}
          aria-pressed={active === zone}
          onClick={() => onSelect(active === zone ? null : zone)}
          className={cn(
            'flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
            active === zone
              ? 'border-primary bg-primary/10 text-foreground'
              : 'border-border text-muted-foreground hover:bg-muted',
          )}
        >
          <StatusGlyph status={ZONE_SAMPLE_STATUS[zone]} />
          <span>{t(ZONE_LABEL_KEY[zone])}</span>
          <span className="metric-mono" data-testid={`board-attention-count-${zone}`}>
            {counts[zone]}
          </span>
        </button>
      ))}
    </div>
  )
}

interface ToolbarProps {
  projects: string[]
  tasks: string[]
  project: string
  task: string
  density: BoardDensity
  shown: number
  total: number
  onProject: (value: string) => void
  onTask: (value: string) => void
  onDensity: (value: BoardDensity) => void
  limit: number
  onLimit: (value: number) => void
}

/**
 * Board controls: two filters and a density toggle.
 *
 * The filter options are derived from the runs actually on the board, not
 * from the full `/v1/projects` / `/v1/tasks` catalogue — filtering to a
 * project with nothing on the board would just produce five empty columns.
 * (The New Run drawer, which needs the values the SERVER accepts rather
 * than the ones currently visible, uses the catalogue endpoints instead.)
 */
function Toolbar({
  projects,
  tasks,
  project,
  task,
  density,
  shown,
  total,
  onProject,
  onTask,
  onDensity,
  limit,
  onLimit,
}: ToolbarProps) {
  const t = useT()

  return (
    <div
      data-testid="run-board-toolbar"
      className="flex flex-wrap items-end gap-3 border-b border-border pb-3"
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="run-filter-project" className="eyebrow">
          {t('common.project')}
        </label>
        <Select
          id="run-filter-project"
          className="min-w-40"
          value={project}
          onChange={(event) => onProject(event.target.value)}
        >
          <option value={ALL}>{t('board.allProjects')}</option>
          {projects.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </Select>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="run-filter-task" className="eyebrow">
          {t('common.task')}
        </label>
        <Select
          id="run-filter-task"
          className="min-w-40"
          value={task}
          onChange={(event) => onTask(event.target.value)}
        >
          <option value={ALL}>{t('board.allTasks')}</option>
          {tasks.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </Select>
      </div>

      <div className="flex flex-col gap-1">
        <span className="eyebrow">{t('board.density')}</span>
        <div className="flex overflow-hidden rounded-md border border-border" data-testid="run-board-density">
          {(['comfortable', 'compact'] as const).map((option) => (
            <button
              key={option}
              type="button"
              data-testid={`run-board-density-${option}`}
              aria-pressed={density === option}
              onClick={() => onDensity(option)}
              className={cn(
                'px-2.5 py-1 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                density === option
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-transparent text-muted-foreground hover:bg-muted',
              )}
            >
              {option === 'comfortable' ? t('board.densityComfortable') : t('board.densityCompact')}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="run-board-limit" className="eyebrow">
          {t('board.limit')}
        </label>
        <Select
          id="run-board-limit"
          data-testid="run-board-limit"
          className="min-w-24"
          value={String(limit)}
          onChange={(event) => onLimit(Number(event.target.value))}
        >
          {RUN_LIMIT_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
      </div>

      <span
        data-testid="run-board-result-count"
        className="metric-mono ml-auto text-xs text-muted-foreground"
      >
        {t('board.showingCount', { shown, total })}
      </span>
    </div>
  )
}

/**
 * Run Board — the Operate section's primary view.
 *
 * Content first: the board fills the view, and "New run" is a header button
 * that opens a drawer (`NewRunDrawer`). The previous layout nailed a
 * permanently-open creation form to the top third of the page, above any
 * content, with free-text project/task boxes for values the server can
 * enumerate.
 *
 * `GET /v1/runs` (tenant-filtered for non-admin roles, see `list_runs` in
 * `api_service.py`), polled every `POLL_INTERVAL_MS` so status transitions
 * show up without a manual refresh. It requires a `run`-rank token (stricter
 * than the token gate's own `read` floor) — a plain `read` token 403s and
 * sees a graceful message.
 *
 * Clicking any card opens `RunDetailPanel` for that run's step-level
 * timeline (which is also where per-step provider/model/token/cost live —
 * the list endpoint carries none of it, so the board never claims to).
 */
export function RunBoardView() {
  const t = useT()
  const { can } = useRole()
  const canRun = can('run')
  const [refreshKey, setRefreshKey] = useState(0)
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [projectFilter, setProjectFilter] = useState<string>(ALL)
  const [taskFilter, setTaskFilter] = useState<string>(ALL)
  const [zoneFilter, setZoneFilter] = useState<AttentionZone | null>(null)
  const [density, setDensity] = usePersistedState<BoardDensity>('pollen.board.density', 'comfortable')
  // How many runs to ask the API for. Persisted like density: an operator
  // watching one pipeline should not have to re-narrow the board on every
  // visit. Bounded server-side (1-500); these are the offered steps.
  const [limit, setLimit] = usePersistedState<number>('pollen.board.limit', DEFAULT_RUN_LIMIT)
  const state = useAsyncData(() => fetchRuns(limit), [refreshKey, limit])
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  // Poll on an interval, cleaned up on unmount (or before the next interval
  // is registered) so a stale timer from a previous mount never leaks. This is
  // now a SAFETY NET behind the realtime SSE subscription below — it still
  // catches up if the stream is unavailable (proxy, network), but the live
  // feed is what makes status transitions appear near-instantly.
  useEffect(() => {
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [])

  // Realtime: refresh the board the moment a run changes (HP-40 bus → HP-41
  // SSE). Coalesced so a burst of step events triggers one refetch, not one
  // per event. Disabled when the board itself is forbidden (a read-only token
  // sees no runs, so there is nothing to keep live).
  const refreshTimer = useRef<number | null>(null)
  useEffect(() => {
    return () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    }
  }, [])
  useEventStream(
    (event) => {
      if (event.entity_type !== 'run') return
      if (refreshTimer.current !== null) return
      refreshTimer.current = window.setTimeout(() => {
        refreshTimer.current = null
        setRefreshKey((key) => key + 1)
      }, 250)
    },
    { enabled: !isForbidden },
  )

  // A module-level constant for the not-yet-loaded case, so `runs` keeps a
  // stable identity between renders and the memos below actually memoize.
  const runs = state.status === 'success' ? state.data : NO_RUNS

  const projects = useMemo(
    () => [...new Set(runs.map((r) => r.project))].sort((a, b) => a.localeCompare(b)),
    [runs],
  )
  const tasks = useMemo(
    () => [...new Set(runs.map((r) => r.task))].sort((a, b) => a.localeCompare(b)),
    [runs],
  )

  const filtered = useMemo(
    () =>
      runs.filter(
        (run) =>
          (projectFilter === ALL || run.project === projectFilter) &&
          (taskFilter === ALL || run.task === taskFilter) &&
          (zoneFilter === null || attentionZone(run.status) === zoneFilter),
      ),
    [runs, projectFilter, taskFilter, zoneFilter],
  )

  function handleRefresh() {
    setRefreshKey((key) => key + 1)
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>{t('nav.runs')}</CardTitle>
          <CardDescription>{canRun ? t('board.description') : t('board.descriptionReadOnly')}</CardDescription>
          {canRun && (
            <CardAction>
              <Button size="sm" className="gap-1.5" onClick={() => setCreating(true)}>
                <Plus className="size-4" />
                {t('runs.newRunButton')}
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {isForbidden && (
            <div
              data-testid="runs-forbidden"
              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              {t('common.requiresRunRankLead')} <span className="font-medium text-foreground">run-rank</span>{' '}
              {t('common.requiresRunRankTail')}
            </div>
          )}

          {!isForbidden && state.status === 'loading' && (
            <div role="status" className="animate-pulse text-sm text-muted-foreground">
              {t('common.loading')}
            </div>
          )}

          {!isForbidden && state.status === 'error' && (
            <div
              role="alert"
              className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
            >
              {describeApiError(state.error)}
            </div>
          )}

          {!isForbidden && state.status === 'success' && runs.length === 0 && (
            <EmptyState
              data-testid="run-board-empty"
              title={t('board.noRunsTitle')}
              body={canRun ? t('board.noRunsBody') : t('board.noRunsBodyReadOnly')}
              action={
                canRun ? (
                  <Button size="sm" className="gap-1.5" onClick={() => setCreating(true)}>
                    <Plus className="size-4" />
                    {t('runs.newRunButton')}
                  </Button>
                ) : undefined
              }
              className="max-w-xl"
            />
          )}

          {!isForbidden && state.status === 'success' && runs.length > 0 && (
            <>
              <AttentionSummary runs={runs} active={zoneFilter} onSelect={setZoneFilter} />
              <Toolbar
                projects={projects}
                tasks={tasks}
                project={projectFilter}
                task={taskFilter}
                density={density}
                shown={filtered.length}
                total={runs.length}
                onProject={setProjectFilter}
                onTask={setTaskFilter}
                onDensity={setDensity}
                limit={limit}
                onLimit={setLimit}
              />

              {filtered.length === 0 ? (
                <EmptyState
                  title={t('board.noMatchTitle')}
                  body={t('board.noMatchBody')}
                  action={
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setProjectFilter(ALL)
                        setTaskFilter(ALL)
                        setZoneFilter(null)
                      }}
                    >
                      {t('board.clearFilters')}
                    </Button>
                  }
                  className="max-w-xl"
                />
              ) : (
                <RunBoard
                  runs={filtered}
                  density={density}
                  canRun={canRun}
                  onOpenDetail={setSelectedRunId}
                  onStopped={handleRefresh}
                />
              )}
            </>
          )}
        </CardContent>
      </Card>

      {creating && canRun && (
        <NewRunDrawer onCreated={handleRefresh} onClose={() => setCreating(false)} />
      )}
      <RunDetailPanel runId={selectedRunId} onClose={() => setSelectedRunId(null)} />
    </>
  )
}
