import { Plus } from 'lucide-react'
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { formatAge, formatElapsed, formatTimestamp } from '@/lib/format-time'
import { useT, type TranslationKey } from '@/lib/i18n'
import { cancelRun, fetchRuns, type RunSummary } from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import {
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

const RUN_LIMIT_OPTIONS = [10, 25, 50, 100, 200] as const
const DEFAULT_RUN_LIMIT = 50

export { type RunColumn, runColumn }

/** Live kanban only. Done is History — never a fifth full-height column. */
export const BOARD_COLUMNS: RunColumn[] = ['queued', 'running', 'waitingApproval', 'failed']

export type RunsSurface = 'board' | 'history'

/**
 * Where a run belongs after the Board / History split.
 *
 * `runColumn` (HP-42) is unchanged. This is a presentation overlay:
 * success/complete → History; cancelled (terminal, not live) → History;
 * paused/deferred stay on the board under Waiting so they remain visible
 * without inventing a fifth column.
 */
export function boardPlacement(status: string): RunColumn | 'history' {
  const column = runColumn(status)
  if (column === 'done') return 'history'
  if (column === 'other') {
    return status.trim().toLowerCase() === 'cancelled' ? 'history' : 'waitingApproval'
  }
  return column
}

export function isHistoryRun(status: string): boolean {
  return boardPlacement(status) === 'history'
}

const COLUMN_LABEL_KEY: Record<(typeof BOARD_COLUMNS)[number], TranslationKey> = {
  queued: 'board.colQueued',
  running: 'board.colRunning',
  waitingApproval: 'board.colWaitingApproval',
  failed: 'board.colFailed',
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
 * Kanban card — title (task), project + id, age. Failed gets a 2px crit
 * left border and no glow. Never renders `RunSummary.detail`.
 */
function RunCard({ run, column, density, canRun, onOpenDetail, onStopped }: RunCardProps) {
  const t = useT()
  const compact = density === 'compact'
  const age = formatAge(run.last_activity_at ?? run.started_at)
  const when = run.last_activity_at ?? run.started_at

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
        'cursor-pointer rounded-[10px] shadow-none transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
        compact ? 'gap-1 p-2' : 'gap-1.5 p-3',
        column === 'failed' && 'border-l-2 border-l-[var(--color-crit)]',
      )}
    >
      <div className={cn('truncate font-medium', compact && 'text-xs')}>{run.task}</div>
      <div className="flex items-baseline justify-between gap-2 text-xs text-muted-foreground">
        <span className="truncate">{run.project}</span>
        <span className="metric-mono shrink-0">#{run.id}</span>
      </div>
      <div className="metric-mono text-xs text-muted-foreground" title={formatTimestamp(when)}>
        {age}
      </div>
      {canRun && run.status === 'running' && <StopButton run={run} onStopped={onStopped} />}
    </Card>
  )
}

interface RunColumnSectionProps {
  column: (typeof BOARD_COLUMNS)[number]
  runs: RunSummary[]
  density: BoardDensity
  canRun: boolean
  onOpenDetail: (runId: number) => void
  onStopped: () => void
}

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
      className="flex min-h-64 min-w-0 flex-1 flex-col gap-2 sm:min-w-[14rem]"
    >
      <div className="flex items-center justify-between gap-2 px-1">
        <h3 className={cn('eyebrow truncate', empty && 'opacity-70')}>{t(COLUMN_LABEL_KEY[column])}</h3>
        <span data-testid={`run-board-count-${column}`} className="metric-mono text-xs text-muted-foreground">
          {runs.length}
        </span>
      </div>
      <div
        className={cn(
          'flex flex-1 flex-col gap-2 rounded-[10px] border border-border bg-card p-2',
          empty && 'items-center justify-center',
        )}
      >
        {empty ? (
          <span aria-hidden="true" className="metric-mono text-sm text-muted-foreground">
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

function RunBoard({ runs, density, canRun, onOpenDetail, onStopped }: RunBoardProps) {
  const t = useT()
  const grouped: Record<(typeof BOARD_COLUMNS)[number], RunSummary[]> = {
    queued: [],
    running: [],
    waitingApproval: [],
    failed: [],
  }
  for (const run of runs) {
    const placement = boardPlacement(run.status)
    if (placement === 'history') continue
    grouped[placement].push(run)
  }

  return (
    <div
      data-testid="run-board-kanban-scroll"
      role="region"
      aria-label={t('board.kanbanScrollLabel')}
      tabIndex={0}
      className="kanban-scroll flex min-w-0 flex-col gap-4 sm:flex-row sm:items-stretch sm:overflow-x-auto sm:pb-2"
    >
      {BOARD_COLUMNS.map((column) => (
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

interface HistoryTableProps {
  runs: RunSummary[]
  onOpenDetail: (runId: number) => void
}

function historySortStamp(run: RunSummary): number {
  const raw = run.finished_at ?? run.started_at
  const ms = Date.parse(raw)
  return Number.isNaN(ms) ? 0 : ms
}

function HistoryTable({ runs, onOpenDetail }: HistoryTableProps) {
  const t = useT()
  const sorted = useMemo(
    () => [...runs].sort((a, b) => historySortStamp(b) - historySortStamp(a)),
    [runs],
  )

  function openFromKeyboard(event: KeyboardEvent<HTMLTableRowElement>, runId: number) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onOpenDetail(runId)
    }
  }

  return (
    <Table scrollLabel={t('board.historyScrollLabel')} data-testid="run-history-table">
      <TableHeader>
        <TableRow>
          <TableHead>{t('common.task')}</TableHead>
          <TableHead>{t('common.project')}</TableHead>
          <TableHead>{t('common.run')}</TableHead>
          <TableHead>{t('common.status')}</TableHead>
          <TableHead>{t('board.age')}</TableHead>
          <TableHead>{t('runs.finished')}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((run) => {
          const duration = run.finished_at ? formatElapsed(run.started_at, run.finished_at) : formatAge(run.started_at)
          return (
            <TableRow
              key={run.id}
              data-testid={`run-history-row-${run.id}`}
              role="button"
              tabIndex={0}
              className="cursor-pointer"
              aria-label={t('board.cardAriaLabel', { id: run.id, task: run.task, project: run.project })}
              onClick={() => onOpenDetail(run.id)}
              onKeyDown={(event) => openFromKeyboard(event, run.id)}
            >
              <TableCell className="font-medium">{run.task}</TableCell>
              <TableCell className="text-muted-foreground">{run.project}</TableCell>
              <TableCell className="metric-mono text-muted-foreground">#{run.id}</TableCell>
              <TableCell>
                <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
              </TableCell>
              <TableCell className="metric-mono text-muted-foreground">{duration}</TableCell>
              <TableCell className="metric-mono text-muted-foreground" title={formatTimestamp(run.finished_at)}>
                {run.finished_at ? formatAge(run.finished_at) : '—'}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

const ALL = '__all__'
const NO_RUNS: RunSummary[] = []

interface ToolbarProps {
  projects: string[]
  tasks: string[]
  statuses?: string[]
  project: string
  task: string
  status?: string
  density?: BoardDensity
  shown: number
  total: number
  onProject: (value: string) => void
  onTask: (value: string) => void
  onStatus?: (value: string) => void
  onDensity?: (value: BoardDensity) => void
  limit: number
  onLimit: (value: number) => void
}

function Toolbar({
  projects,
  tasks,
  statuses,
  project,
  task,
  status,
  density,
  shown,
  total,
  onProject,
  onTask,
  onStatus,
  onDensity,
  limit,
  onLimit,
}: ToolbarProps) {
  const t = useT()

  return (
    <div data-testid="run-board-toolbar" className="flex flex-wrap items-end gap-3">
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

      {statuses && onStatus && status !== undefined && (
        <div className="flex flex-col gap-1">
          <label htmlFor="run-filter-status" className="eyebrow">
            {t('common.status')}
          </label>
          <Select
            id="run-filter-status"
            data-testid="run-history-status-filter"
            className="min-w-32"
            value={status}
            onChange={(event) => onStatus(event.target.value)}
          >
            <option value={ALL}>{t('board.allStatuses')}</option>
            {statuses.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </div>
      )}

      {density && onDensity && (
        <div className="flex flex-col gap-1">
          <span className="eyebrow">{t('board.density')}</span>
          <div className="flex overflow-hidden rounded-[10px] border border-border" data-testid="run-board-density">
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
      )}

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

      <span data-testid="run-board-result-count" className="metric-mono ml-auto text-xs text-muted-foreground">
        {t('board.showingCount', { shown, total })}
      </span>
    </div>
  )
}

interface SurfaceTabsProps {
  surface: RunsSurface
  onSurface: (value: RunsSurface) => void
}

function SurfaceTabs({ surface, onSurface }: SurfaceTabsProps) {
  const t = useT()
  return (
    <div
      data-testid="runs-surface-tabs"
      role="tablist"
      aria-label={t('board.tabsLabel')}
      className="flex overflow-hidden rounded-full border border-border"
    >
      {(['board', 'history'] as const).map((option) => (
        <button
          key={option}
          type="button"
          role="tab"
          data-testid={`runs-surface-${option}`}
          aria-selected={surface === option}
          onClick={() => onSurface(option)}
          className={cn(
            'px-3 py-1 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
            surface === option
              ? 'bg-muted text-foreground'
              : 'bg-transparent text-muted-foreground hover:text-foreground',
          )}
        >
          {option === 'board' ? t('board.tabBoard') : t('board.tabHistory')}
        </button>
      ))}
    </div>
  )
}

/**
 * Runs — Board of live work, History of completed runs (redesign mock B).
 *
 * Done never occupies a kanban column. The four live columns stay Queued /
 * Running / Waiting / Failed. New run stays a header CTA.
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
  const [statusFilter, setStatusFilter] = useState<string>(ALL)
  const [surface, setSurface] = usePersistedState<RunsSurface>('pollen.runs.surface', 'board')
  const [density, setDensity] = usePersistedState<BoardDensity>('pollen.board.density', 'comfortable')
  const [limit, setLimit] = usePersistedState<number>('pollen.board.limit', DEFAULT_RUN_LIMIT)
  const state = useAsyncData(() => fetchRuns(limit), [refreshKey, limit])
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  useEffect(() => {
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [])

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

  const runs = state.status === 'success' ? state.data : NO_RUNS
  const boardRuns = useMemo(() => runs.filter((run) => !isHistoryRun(run.status)), [runs])
  const historyRuns = useMemo(() => runs.filter((run) => isHistoryRun(run.status)), [runs])
  const surfaceRuns = surface === 'board' ? boardRuns : historyRuns

  const projects = useMemo(
    () => [...new Set(surfaceRuns.map((r) => r.project))].sort((a, b) => a.localeCompare(b)),
    [surfaceRuns],
  )
  const tasks = useMemo(
    () => [...new Set(surfaceRuns.map((r) => r.task))].sort((a, b) => a.localeCompare(b)),
    [surfaceRuns],
  )
  const historyStatuses = useMemo(
    () => [...new Set(historyRuns.map((r) => r.status))].sort((a, b) => a.localeCompare(b)),
    [historyRuns],
  )

  const filtered = useMemo(
    () =>
      surfaceRuns.filter(
        (run) =>
          (projectFilter === ALL || run.project === projectFilter) &&
          (taskFilter === ALL || run.task === taskFilter) &&
          (surface === 'board' || statusFilter === ALL || run.status === statusFilter),
      ),
    [surfaceRuns, projectFilter, taskFilter, statusFilter, surface],
  )

  function handleRefresh() {
    setRefreshKey((key) => key + 1)
  }

  function clearFilters() {
    setProjectFilter(ALL)
    setTaskFilter(ALL)
    setStatusFilter(ALL)
  }

  function openHistory() {
    setSurface('history')
    clearFilters()
  }

  return (
    <div className="flex w-full flex-col gap-6" data-testid="runs-page">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-3xl font-semibold tracking-tight">{t('nav.runs')}</h2>
          <p className="text-sm text-muted-foreground">
            {canRun ? t('board.subtitle') : t('board.subtitleReadOnly')}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SurfaceTabs
            surface={surface}
            onSurface={(value) => {
              setSurface(value)
              clearFilters()
            }}
          />
          {canRun && (
            <Button size="sm" className="gap-1.5 rounded-full" onClick={() => setCreating(true)}>
              <Plus className="size-4" />
              {t('runs.newRunButton')}
            </Button>
          )}
        </div>
      </header>

      {isForbidden && (
        <div
          data-testid="runs-forbidden"
          className="rounded-[10px] border border-border bg-card p-3 text-sm text-muted-foreground"
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
          className="rounded-[10px] border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
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
              <Button size="sm" className="gap-1.5 rounded-full" onClick={() => setCreating(true)}>
                <Plus className="size-4" />
                {t('runs.newRunButton')}
              </Button>
            ) : undefined
          }
          className="max-w-xl"
        />
      )}

      {!isForbidden && state.status === 'success' && runs.length > 0 && (
        <div className="flex flex-col gap-4">
          {surface === 'board' && historyRuns.length > 0 && (
            <button
              type="button"
              data-testid="runs-done-shortcut"
              onClick={openHistory}
              className="self-start text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            >
              {t('board.doneShortcut', { count: historyRuns.length })}
            </button>
          )}

          {surface === 'history' && (
            <p data-testid="run-history-caption" className="text-sm text-muted-foreground">
              {t('board.historyCaption', { count: historyRuns.length })}
            </p>
          )}

          <Toolbar
            projects={projects}
            tasks={tasks}
            statuses={surface === 'history' ? historyStatuses : undefined}
            project={projectFilter}
            task={taskFilter}
            status={surface === 'history' ? statusFilter : undefined}
            density={surface === 'board' ? density : undefined}
            shown={filtered.length}
            total={surfaceRuns.length}
            onProject={setProjectFilter}
            onTask={setTaskFilter}
            onStatus={surface === 'history' ? setStatusFilter : undefined}
            onDensity={surface === 'board' ? setDensity : undefined}
            limit={limit}
            onLimit={setLimit}
          />

          {surface === 'board' && boardRuns.length > 0 && filtered.length === 0 && (
            <EmptyState
              title={t('board.noMatchTitle')}
              body={t('board.noMatchBody')}
              action={
                <Button size="sm" variant="outline" onClick={clearFilters}>
                  {t('board.clearFilters')}
                </Button>
              }
              className="max-w-xl"
            />
          )}

          {surface === 'board' && (boardRuns.length === 0 || filtered.length > 0) && (
            <RunBoard
              runs={filtered}
              density={density}
              canRun={canRun}
              onOpenDetail={setSelectedRunId}
              onStopped={handleRefresh}
            />
          )}

          {surface === 'history' && historyRuns.length === 0 && (
            <p data-testid="run-history-empty" className="text-sm text-muted-foreground">
              {t('board.historyEmpty')}
            </p>
          )}

          {surface === 'history' && historyRuns.length > 0 && filtered.length === 0 && (
            <EmptyState
              title={t('board.noMatchTitle')}
              body={t('board.noMatchBody')}
              action={
                <Button size="sm" variant="outline" onClick={clearFilters}>
                  {t('board.clearFilters')}
                </Button>
              }
              className="max-w-xl"
            />
          )}

          {surface === 'history' && filtered.length > 0 && (
            <HistoryTable runs={filtered} onOpenDetail={setSelectedRunId} />
          )}
        </div>
      )}

      {creating && canRun && <NewRunDrawer onCreated={handleRefresh} onClose={() => setCreating(false)} />}
      <RunDetailPanel runId={selectedRunId} onClose={() => setSelectedRunId(null)} />
    </div>
  )
}
