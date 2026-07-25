import { type FormEvent, type KeyboardEvent, useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT, type TranslationKey } from '@/lib/i18n'
import { cancelRun, createRun, fetchRuns, type RunSummary } from '@/lib/mirador-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'
import { RunDetailPanel } from './RunDetailPanel'

/** Poll cadence for `GET /v1/runs` — status transitions (running ->
 * success/failed/pending) show up without a manual refresh. Must stay
 * `<= 3000` per the sprint's acceptance criteria. */
const POLL_INTERVAL_MS = 3000

// ---------------------------------------------------------------------------
// Status -> Kanban column mapping. Mirrors `hivepilot/services/
// analytics_service.py`'s canonical status classification (its
// `_SUCCEEDED_STATUSES`/`_FAILED_STATUSES`/`_SKIPPED_STATUSES` sets and the
// comment above them listing every non-terminal state) AND `state_service.
// py`'s `RunStatus` enum + its `"pending"` legacy alias / `api_service.py`'s
// `create_run` (which stores that literal directly). Read both before
// changing any set below — this view NEVER invents a status the backend
// doesn't already use.
// ---------------------------------------------------------------------------

export type RunColumn = 'queued' | 'running' | 'waitingApproval' | 'failed' | 'done' | 'other'

/** Pre-execution states: the formal `RunStatus.NEW`/`RunStatus.PLANNED`
 * enum values, plus the literal `"pending"` `create_run` stores directly
 * for a require-approval initial run (see `api_service.py`) — `"pending"`
 * is ALSO `RunStatus.from_str`'s legacy alias for `NEW`. */
const QUEUED_STATUSES = new Set(['new', 'planned', 'pending'])
/** A human decision is needed: the formal `RunStatus.APPROVAL`/`RunStatus.
 * REVIEW` enum values, plus the literal `"awaiting_approval"` orchestrator.
 * py sets at its own approval checkpoint. */
const WAITING_APPROVAL_STATUSES = new Set(['approval', 'awaiting_approval', 'review'])
/** Mirrors `analytics_service.py`'s `_FAILED_STATUSES` exactly. */
const FAILED_STATUSES = new Set([
  'failed',
  'denied',
  'rate_limit',
  'auth_expired',
  'test_failure',
  'security_blocker',
])
/** Mirrors `analytics_service.py`'s `_SUCCEEDED_STATUSES` exactly. */
const DONE_STATUSES = new Set(['success', 'complete'])

/**
 * Maps a real `RunSummary.status` to a Kanban column — faithfully, never
 * inventing a status. Anything not in one of the four sets above (`paused`
 * — an operator mid-run pause, `cancelled` — operator-stopped, `deferred`
 * — quota/backoff retry-later, or a genuinely unrecognized string) lands in
 * `'other'`: exactly the same "everything else" bucket `analytics_service.
 * py`'s own `canonical_outcome()` falls back to for these same statuses —
 * this view never asserts a stronger classification than the backend
 * itself does.
 */
export function runColumn(status: string): RunColumn {
  const normalised = status.trim().toLowerCase()
  if (QUEUED_STATUSES.has(normalised)) return 'queued'
  if (normalised === 'running') return 'running'
  if (WAITING_APPROVAL_STATUSES.has(normalised)) return 'waitingApproval'
  if (FAILED_STATUSES.has(normalised)) return 'failed'
  if (DONE_STATUSES.has(normalised)) return 'done'
  return 'other'
}

const COLUMN_ORDER: RunColumn[] = ['queued', 'running', 'waitingApproval', 'failed', 'done']

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

function formatDurationSeconds(totalSeconds: number): string {
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  if (days > 0) return `${days}d`
  if (hours > 0) return `${hours}h`
  if (minutes > 0) return `${minutes}m`
  return `${totalSeconds}s`
}

/** Elapsed time from `iso` to now, as a short "2d"/"3h"/"12m"/"45s" string —
 * mirrors `HomeView`'s own `formatAge` (each view owns a small copy of this
 * formatting helper, same convention as `formatTimestamp` above).
 * Unparseable/missing input renders as "—", never a fabricated age. */
function formatAge(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return formatDurationSeconds(Math.max(0, Math.round((Date.now() - date.getTime()) / 1000)))
}

/** Elapsed time BETWEEN two timestamps (a finished run's actual duration),
 * same short-string format as `formatAge`. `null`/unparseable either side
 * renders "—", never a fabricated duration. */
function formatElapsed(startIso: string | null | undefined, endIso: string | null | undefined): string {
  if (!startIso || !endIso) return '—'
  const start = new Date(startIso)
  const end = new Date(endIso)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '—'
  return formatDurationSeconds(Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000)))
}

function statusVariant(status: string): 'default' | 'secondary' | 'destructive' {
  const normalised = status.trim().toLowerCase()
  if (DONE_STATUSES.has(normalised)) return 'default'
  if (FAILED_STATUSES.has(normalised) || normalised === 'cancelled') return 'destructive'
  return 'secondary'
}

interface NewRunFormProps {
  onCreated: () => void
}

/**
 * New Run form — only rendered by the parent (`RunBoardView` below) when
 * `useRole().can('run')` (defense-in-depth; `POST /v1/runs` enforces the
 * same `run` role server-side regardless of what the client shows, see
 * `create_run` in `api_service.py`). Task/project are required client-side;
 * extra_prompt/auto_git are optional. `POST /v1/runs` is asynchronous — it
 * returns 202 immediately and the pipeline runs on a background thread
 * server-side — so submission resolves fast regardless of how long the
 * triggered run itself takes; `onCreated` forces an immediate board refresh
 * instead of waiting for the next poll tick.
 */
function NewRunForm({ onCreated }: NewRunFormProps) {
  const t = useT()
  const [task, setTask] = useState('')
  const [project, setProject] = useState('')
  const [extraPrompt, setExtraPrompt] = useState('')
  const [autoGit, setAutoGit] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canSubmit = task.trim().length > 0 && project.trim().length > 0 && !submitting

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createRun({
        task: task.trim(),
        project: project.trim(),
        extra_prompt: extraPrompt.trim() ? extraPrompt.trim() : undefined,
        auto_git: autoGit,
      })
      setTask('')
      setProject('')
      setExtraPrompt('')
      setAutoGit(false)
      onCreated()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('runs.insufficientRoleCreate') : describeApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mb-6 flex flex-col gap-3 border-b border-border pb-6">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor="new-run-task" className="text-sm font-medium">
            {t('common.task')}
          </label>
          <Input
            id="new-run-task"
            value={task}
            onChange={(event) => setTask(event.target.value)}
            placeholder={t('runs.taskPlaceholder')}
            required
            disabled={submitting}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="new-run-project" className="text-sm font-medium">
            {t('common.project')}
          </label>
          <Input
            id="new-run-project"
            value={project}
            onChange={(event) => setProject(event.target.value)}
            placeholder={t('runs.projectPlaceholder')}
            required
            disabled={submitting}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new-run-extra-prompt" className="text-sm font-medium">
          {t('runs.extraPromptLabel')}
        </label>
        <textarea
          id="new-run-extra-prompt"
          value={extraPrompt}
          onChange={(event) => setExtraPrompt(event.target.value)}
          placeholder={t('runs.extraPromptPlaceholder')}
          disabled={submitting}
          rows={3}
          className="w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={autoGit}
          onChange={(event) => setAutoGit(event.target.checked)}
          disabled={submitting}
          className="size-4 rounded border-input"
        />
        {t('runs.autoGitLabel')}
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!canSubmit}>
          {submitting ? t('common.starting') : t('runs.newRunButton')}
        </Button>
        {submitting && (
          <span role="status" className="text-sm text-muted-foreground">
            {t('common.starting')}
          </span>
        )}
      </div>
      {error && (
        <div role="alert" className="text-sm text-destructive">
          {error}
        </div>
      )}
    </form>
  )
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

interface RunCardProps {
  run: RunSummary
  column: RunColumn
  canRun: boolean
  onOpenDetail: (runId: number) => void
  onStopped: () => void
}

/**
 * One Kanban card — project·task, status badge, and age (running/queued) or
 * actual duration (terminal, when `finished_at` is present). Never renders
 * `RunSummary.detail` (untrusted free text, same caveat as `Approval.
 * metadata` elsewhere in this app) -- only the typed, structural fields.
 * The whole card is clickable (opens `RunDetailPanel` for this run);
 * keyboard-operable via `role="button"`/`tabIndex`/Enter-or-Space (this is
 * a `div`, not a native `<button>`, because it also hosts a real nested
 * `<button>` -- the Stop control -- which native button-in-button nesting
 * disallows).
 */
function RunCard({ run, column, canRun, onOpenDetail, onStopped }: RunCardProps) {
  const t = useT()

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
      className={cn('cursor-pointer gap-2 p-3 transition-colors hover:bg-muted/50', STRIPE_CLASS[column])}
    >
      <div className="flex flex-col gap-1 text-sm">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-medium">{run.project}</span>
          <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
        </div>
        <span className="truncate text-muted-foreground">{run.task}</span>
        <span className="text-xs text-muted-foreground">
          {run.finished_at
            ? t('board.duration', { duration: formatElapsed(run.started_at, run.finished_at) })
            : t('board.startedAgo', { age: formatAge(run.started_at) })}
        </span>
      </div>
      {canRun && run.status === 'running' && <StopButton run={run} onStopped={onStopped} />}
    </Card>
  )
}

interface RunColumnSectionProps {
  column: RunColumn
  runs: RunSummary[]
  canRun: boolean
  onOpenDetail: (runId: number) => void
  onStopped: () => void
}

function RunColumnSection({ column, runs, canRun, onOpenDetail, onStopped }: RunColumnSectionProps) {
  const t = useT()
  return (
    <div data-testid={`run-board-column-${column}`} className="flex flex-col gap-2 sm:w-72 sm:shrink-0">
      <div className="flex items-center justify-between px-1">
        <h3 className="text-sm font-semibold">{t(COLUMN_LABEL_KEY[column])}</h3>
        <Badge variant="outline" data-testid={`run-board-count-${column}`}>
          {runs.length}
        </Badge>
      </div>
      <div className="flex min-h-16 flex-col gap-2 rounded-lg bg-muted/30 p-2">
        {runs.length === 0 ? (
          <p className="px-1 text-xs text-muted-foreground">{t('board.columnEmpty')}</p>
        ) : (
          runs.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              column={column}
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
 * row (fixed-width columns) -- a Kanban board's natural responsive shape.
 */
function RunBoard({ runs, canRun, onOpenDetail, onStopped }: RunBoardProps) {
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
    <div className="flex flex-col gap-4 sm:flex-row sm:overflow-x-auto sm:pb-2">
      {columns.map((column) => (
        <RunColumnSection
          key={column}
          column={column}
          runs={grouped[column]}
          canRun={canRun}
          onOpenDetail={onOpenDetail}
          onStopped={onStopped}
        />
      ))}
    </div>
  )
}

/**
 * Run Board — the Mirador Operate section's primary view (Mirador Operate
 * section PRD: replaces the flat `RunsView` table with a Kanban board,
 * demoting the node-graph out of top-level prominence -- see
 * `nav-config.ts`). `GET /v1/runs` (tenant-filtered for non-admin roles,
 * see `list_runs` in `api_service.py`), polled every `POLL_INTERVAL_MS` so
 * status transitions show up without a manual refresh. A New Run form
 * (`POST /v1/runs`, async — 202 + background execution) is shown only for
 * `useRole().can('run')` — the server enforces the same `run` role
 * regardless of what the client shows.
 *
 * `GET /v1/runs` itself requires a `run`-rank token (stricter than the
 * token gate's own `read` floor) — a plain `read` token 403s and sees a
 * graceful message (same carve-out pattern as `ApprovalsView`/`Mem0View`/
 * `GraphView` — none of which route their endpoint-specific 403 message
 * through `AsyncSection`, which only knows the generic error case).
 *
 * Clicking any card opens `RunDetailPanel` (`GET /v1/runs/{run_id}`) for
 * that run's step-level timeline. Card-level runner/model is deliberately
 * NOT shown -- `RunSummary` (the list endpoint) has no such field, only
 * per-step `RunDetail.steps[].provider`/`.model` does; fetching per-card
 * detail for the whole board would mean N detail requests per poll tick
 * for N cards, which this view does not do -- runner/model is available
 * once a card is opened, in the drill-down.
 */
export function RunBoardView() {
  const t = useT()
  const { can } = useRole()
  const canRun = can('run')
  const [refreshKey, setRefreshKey] = useState(0)
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const state = useAsyncData(() => fetchRuns(), [refreshKey])
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  // Poll on an interval, cleaned up on unmount (or before the next interval
  // is registered) so a stale timer from a previous mount never leaks.
  useEffect(() => {
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [])

  function handleCreated() {
    setRefreshKey((key) => key + 1)
  }

  function handleStopped() {
    setRefreshKey((key) => key + 1)
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>{t('nav.runs')}</CardTitle>
          <CardDescription>{canRun ? t('board.description') : t('board.descriptionReadOnly')}</CardDescription>
        </CardHeader>
        <CardContent>
          {canRun && <NewRunForm onCreated={handleCreated} />}

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

          {!isForbidden && state.status === 'success' && state.data.length === 0 && (
            <p data-testid="run-board-empty" className="text-sm text-muted-foreground">
              {t('board.noRunsAtAll')}
            </p>
          )}

          {!isForbidden && state.status === 'success' && state.data.length > 0 && (
            <RunBoard
              runs={state.data}
              canRun={canRun}
              onOpenDetail={setSelectedRunId}
              onStopped={handleStopped}
            />
          )}
        </CardContent>
      </Card>
      <RunDetailPanel runId={selectedRunId} onClose={() => setSelectedRunId(null)} />
    </>
  )
}
