import { AlertTriangle, GitBranch, Layers, ShieldAlert, Split } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { SectionHeader } from '@/components/dashboard/SectionHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Drawer } from '@/components/ui/drawer'
import { Input } from '@/components/ui/input'
import { ApiError, ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { EM_DASH, formatAge, formatDurationSeconds } from '@/lib/format-time'
import { type TranslationKey, useT } from '@/lib/i18n'
import {
  type PartitionDetail,
  type PartitionParallelism,
  type PartitionPreview,
  type PartitionSummary,
  fetchPartition,
  fetchPartitions,
  previewPartition,
  ratifyPartition,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'
import { AsyncSection } from './AsyncSection'

/** How long the editor stays quiet before re-running the server-side dry run.
 * The preview is a real HTTP round trip that resolves live pipeline config, so
 * it must not fire per keystroke; 400ms is short enough that the verdict feels
 * attached to the edit and long enough that typing a task id isn't 12 calls. */
export const PREVIEW_DEBOUNCE_MS = 400

/** The outward-action vocabulary is CLOSED and defined by the engine (spec
 * §6). This maps each token to a human clause for the consent sentence — the
 * wording is local, the LIST is not: it always comes from the backend's
 * computed footprint. An unrecognized token renders VERBATIM rather than being
 * dropped or guessed at, so a vocabulary the engine grows can never silently
 * disappear from the warning the operator consents to. */
const OUTWARD_ACTION_LABEL: Record<string, TranslationKey> = {
  git_push: 'partitions.outwardAction.git_push',
  forge_pr: 'partitions.outwardAction.forge_pr',
  forge_merge: 'partitions.outwardAction.forge_merge',
  forge_issue: 'partitions.outwardAction.forge_issue',
  forge_release: 'partitions.outwardAction.forge_release',
  notify: 'partitions.outwardAction.notify',
  vault_write: 'partitions.outwardAction.vault_write',
  external_api: 'partitions.outwardAction.external_api',
}

/** Tokens the engine names in the warning but does NOT yet suppress at
 * runtime (spec §6: v1 threads `auto_git` only). Declared loudly here rather
 * than letting the checkbox imply coverage it doesn't have. */
const OUTWARD_TOKENS_NOT_ENFORCED_AT_RUNTIME = ['notify', 'vault_write', 'external_api']

/** Only a `proposed` partition can be ratified — the transition is a
 * conditional `UPDATE ... WHERE status='proposed'` server-side. This is a
 * display gate over that fact, never a substitute for it. */
const RATIFIABLE_STATUS = 'proposed'

type ParseResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; message: string }

/** Client-side syntax check ONLY. It answers "is this JSON at all", which is
 * the one question that can be answered honestly without the server. Every
 * SEMANTIC question — is this pipeline real, is this budget within policy,
 * what becomes visible outside this machine — is the gate's, and is asked via
 * `previewPartition`. */
function parsePlan(text: string): ParseResult {
  try {
    const value: unknown = JSON.parse(text)
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { ok: false, message: 'Expected a JSON object at the top level.' }
    }
    return { ok: true, value: value as Record<string, unknown> }
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
}

/** Re-indent the stored document for editing. Same document, same digest
 * contract: `expected_digest` is the value the server handed us, and the
 * ratified digest is computed over whatever we submit — so formatting is
 * free. Unparseable stored JSON is shown VERBATIM (never "fixed"). */
function forEditing(raw: string): string {
  const parsed = parsePlan(raw)
  return parsed.ok ? JSON.stringify(parsed.value, null, 2) : raw
}

interface EditableTask {
  id?: unknown
  title?: unknown
  project?: unknown
  pipeline?: unknown
  depends_on?: unknown
  budget?: unknown
  [key: string]: unknown
}

/** The tasks array, or `[]` when the document doesn't have one in the shape
 * the typed controls can operate on. Deliberately assumes NOTHING: the box is
 * free-form and an operator may put anything in it; the controls simply have
 * nothing to offer for a shape they can't read, and the raw box stays. */
function readTasks(plan: Record<string, unknown>): EditableTask[] {
  const tasks = plan.tasks
  if (!Array.isArray(tasks)) return []
  return tasks.filter((task): task is EditableTask => task !== null && typeof task === 'object')
}

function taskIdOf(task: EditableTask): string {
  return typeof task.id === 'string' ? task.id : ''
}

function readBudgetNumber(task: EditableTask, field: string): number | null {
  const budget = task.budget
  if (budget === null || typeof budget !== 'object') return null
  const value = (budget as Record<string, unknown>)[field]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function readDependsOn(task: EditableTask): string[] {
  return Array.isArray(task.depends_on)
    ? task.depends_on.filter((id): id is string => typeof id === 'string')
    : []
}

/** Joins clauses the way the language does: "a and b", "a, b and c". */
function joinClauses(clauses: string[], and: string): string {
  if (clauses.length <= 1) return clauses.join('')
  return `${clauses.slice(0, -1).join(', ')}${and}${clauses[clauses.length - 1]}`
}

interface BudgetFieldProps {
  label: string
  ariaLabel: string
  value: number | null
  step: string
  onCommit: (raw: string) => void
  disabled: boolean
}

/**
 * One budget number, committed on blur rather than per keystroke.
 *
 * Per-keystroke commit round-trips through `JSON.stringify`, which turns the
 * intermediate `"1."` of typing `1.5` into `1` and eats the character the
 * operator just typed. Holding a local draft and committing on blur avoids
 * that entirely, and keeps the raw JSON box authoritative: an edit made THERE
 * flows back down here through `value`.
 */
function BudgetField({ label, ariaLabel, value, step, onCommit, disabled }: BudgetFieldProps) {
  const [draft, setDraft] = useState(value === null ? '' : String(value))

  // Syncs a change made in the raw JSON box back into this field. It cannot
  // clobber in-progress typing: `value` only moves when the document does,
  // and this field doesn't write to the document until blur.
  useEffect(() => {
    setDraft(value === null ? '' : String(value))
  }, [value])

  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-[10px] font-semibold tracking-wider text-muted-foreground uppercase">
        {label}
      </span>
      <Input
        type="number"
        min="0"
        step={step}
        inputMode="decimal"
        className="touch-target h-9"
        aria-label={ariaLabel}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => onCommit(draft)}
      />
    </label>
  )
}

interface TaskControlsProps {
  tasks: EditableTask[]
  disabled: boolean
  onDrop: (taskId: string) => void
  onBudgetChange: (taskId: string, field: string, raw: string) => void
}

/**
 * Typed controls for the two things operators actually change: drop a task,
 * change a budget (spec §12.2). They sit ALONGSIDE the raw JSON box and write
 * into the same document — the box is kept because it is the honest escape
 * hatch that shows exactly what will run.
 *
 * Dropping a task that others depend on deliberately leaves their
 * `depends_on` dangling: the engine's own `UnknownTaskDependencyError` reports
 * it through the preview, in its own words. Silently rewriting other tasks'
 * dependencies would be this view inventing a plan the operator didn't write.
 */
function TaskControls({ tasks, disabled, onDrop, onBudgetChange }: TaskControlsProps) {
  const t = useT()

  if (tasks.length === 0) {
    return (
      <EmptyState
        data-testid="partitions-no-tasks"
        icon={<Layers className="size-4" />}
        title={t('partitions.noTasks')}
        body={t('partitions.noTasksBody')}
      />
    )
  }

  return (
    <ul className="flex flex-col gap-3">
      {tasks.map((task, index) => {
        const id = taskIdOf(task)
        const wallClock = readBudgetNumber(task, 'wall_clock_seconds')
        const cost = readBudgetNumber(task, 'cost_usd')
        const dependsOn = readDependsOn(task)
        return (
          <li
            key={id || `task-${index}`}
            data-testid={`partition-task-${id || index}`}
            className="flex flex-col gap-3 rounded-lg border border-border p-3"
          >
            <div className="flex flex-wrap items-start gap-2">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="metric-mono text-sm break-words">{id || EM_DASH}</span>
                {typeof task.title === 'string' && task.title !== '' && (
                  <span className="text-xs break-words text-muted-foreground">{task.title}</span>
                )}
                {dependsOn.length > 0 && (
                  <span className="text-xs break-words text-muted-foreground">
                    {t('partitions.dependsOn', { ids: dependsOn.join(', ') })}
                  </span>
                )}
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="touch-target ml-auto"
                disabled={disabled || id === ''}
                aria-label={t('partitions.dropTaskAriaLabel', { id })}
                onClick={() => onDrop(id)}
              >
                {t('partitions.dropTask')}
              </Button>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <BudgetField
                label={`${t('partitions.wallClockLabel')} ${
                  wallClock === null ? EM_DASH : formatDurationSeconds(wallClock)
                }`}
                ariaLabel={t('partitions.wallClockAriaLabel', { id })}
                value={wallClock}
                step="1"
                disabled={disabled}
                onCommit={(raw) => onBudgetChange(id, 'wall_clock_seconds', raw)}
              />
              <BudgetField
                label={t('partitions.costLabel')}
                ariaLabel={t('partitions.costAriaLabel', { id })}
                value={cost}
                step="0.01"
                disabled={disabled}
                onCommit={(raw) => onBudgetChange(id, 'cost_usd', raw)}
              />
            </div>
          </li>
        )
      })}
    </ul>
  )
}

interface OutwardConsentProps {
  actions: string[]
  consent: boolean
  disabled: boolean
  onChange: (next: boolean) => void
}

/**
 * The separate consent axis (spec §6). It is NOT the same question as "do I
 * ratify this plan": ratification says the plan is right, consent says it may
 * become visible outside this machine.
 *
 * Unticked by default, always. `actions` is the footprint the BACKEND
 * computed from live pipeline config for the plan currently in the editor —
 * never a fixed string, and never the plan's own `outward` flags (which are
 * proposal-declared, i.e. editable in the very box this gate polices).
 */
function OutwardConsent({ actions, consent, disabled, onChange }: OutwardConsentProps) {
  const t = useT()

  if (actions.length === 0) {
    return (
      <EmptyState
        data-testid="partitions-outward-none"
        icon={<ShieldAlert className="size-4" />}
        title={t('partitions.outwardNoneTitle')}
        body={t('partitions.outwardNoneBody')}
      />
    )
  }

  const clauses = actions.map((action) => {
    const key = OUTWARD_ACTION_LABEL[action]
    return key ? t(key) : action
  })
  const unenforced = actions.filter((action) =>
    OUTWARD_TOKENS_NOT_ENFORCED_AT_RUNTIME.includes(action),
  )

  return (
    <div
      data-testid="partitions-outward-consent"
      className="flex flex-col gap-3 rounded-lg border border-l-4 border-border border-l-[var(--color-warn)] bg-muted/20 p-3"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-[var(--color-warn)]" />
        <p data-testid="partitions-outward-warning" className="text-sm break-words text-foreground">
          {t('partitions.outwardWarning', { actions: joinClauses(clauses, t('common.and')) })}
        </p>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {actions.map((action) => (
          <Badge key={action} variant="outline" className="metric-mono">
            {action}
          </Badge>
        ))}
      </div>
      <label className="touch-target flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-0.5 size-4 shrink-0 accent-[var(--color-warn)]"
          checked={consent}
          disabled={disabled}
          aria-label={t('partitions.outwardConsentLabel')}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="break-words">{t('partitions.outwardConsentLabel')}</span>
      </label>
      {unenforced.length > 0 && (
        <p data-testid="partitions-outward-gap" className="text-xs break-words text-muted-foreground">
          {t('partitions.outwardV1Gap')}
        </p>
      )}
      <p className="text-xs break-words text-muted-foreground">{t('partitions.outwardHonesty')}</p>
    </div>
  )
}

/**
 * The computed number, never the requested one.
 *
 * `claude_max_concurrency` defaults to 1, so "3 parallel agents" is one agent
 * three times on a default install. `notes` carries the backend's own
 * explanation and is rendered verbatim. A `null` assessment renders an
 * em-dash — the value could not be computed, and a plausible-looking `1`
 * would be a fabrication.
 */
function ParallelismReadout({ parallelism }: { parallelism: PartitionParallelism | null }) {
  const t = useT()
  const throttled = parallelism !== null && parallelism.effective < parallelism.requested

  return (
    <div className="flex flex-col gap-2">
      <div data-testid="partitions-parallelism">
        <MetricReadout
          icon={<Split className="size-4" />}
          label={t('partitions.parallelismLabel')}
          value={parallelism === null ? EM_DASH : parallelism.effective}
          sub={
            parallelism === null
              ? undefined
              : t('partitions.parallelismSub', { requested: parallelism.requested })
          }
          tone={throttled ? 'warn' : 'default'}
        />
      </div>
      {parallelism !== null && parallelism.notes.length > 0 && (
        <ul data-testid="partitions-parallelism-notes" className="flex flex-col gap-1">
          {parallelism.notes.map((note) => (
            <li key={note} className="text-xs break-words text-muted-foreground">
              {note}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

interface RatifyFormProps {
  detail: PartitionDetail
  onRatified: () => void
}

/**
 * The ratification form itself, mounted once the partition has resolved so
 * every piece of state can be initialised from real data rather than
 * back-filled by an effect.
 *
 * The gate is NOT re-implemented here. `previewPartition` dry-runs the same
 * `validate_ratification` the real ratification runs, and `preview.ok` is the
 * single thing the dispatch control gates on (alongside a local JSON syntax
 * check, which is the one question answerable without the server). The
 * outward footprint, the cost sum, the wave plan and the effective
 * parallelism are all read from that response.
 */
function RatifyForm({ detail, onRatified }: RatifyFormProps) {
  const t = useT()
  const [text, setText] = useState(() => forEditing(detail.proposed_json ?? ''))
  const [debounced, setDebounced] = useState(() => forEditing(detail.proposed_json ?? ''))
  const [consent, setConsent] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)

  const parsed = useMemo(() => parsePlan(text), [text])
  const parsedDebounced = useMemo(() => parsePlan(debounced), [debounced])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(text), PREVIEW_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [text])

  // A plan that isn't JSON has nothing for the gate to check — the local
  // parse error is the honest answer, and skipping the call also keeps a
  // half-typed document from generating a request per pause.
  const preview = useAsyncData<PartitionPreview | null>(
    () =>
      parsedDebounced.ok
        ? previewPartition(detail.id, debounced, consent)
        : Promise.resolve(null),
    [detail.id, debounced, consent, parsedDebounced.ok],
  )
  const verdict = preview.status === 'success' ? preview.data : null
  const previewFailed = preview.status === 'error'
  // Fail-closed: dispatch is enabled only when the plan parses locally AND the
  // GATE ITSELF said yes. A preview that errored, is still in flight, or was
  // skipped leaves this false.
  const gateAccepts = verdict !== null && verdict.ok
  const canDispatch = parsed.ok && gateAccepts && !submitting

  function replacePlan(next: Record<string, unknown>) {
    setText(JSON.stringify(next, null, 2))
  }

  function handleDrop(taskId: string) {
    if (!parsed.ok) return
    replacePlan({
      ...parsed.value,
      tasks: readTasks(parsed.value).filter((task) => taskIdOf(task) !== taskId),
    })
  }

  function handleBudgetChange(taskId: string, field: string, raw: string) {
    if (!parsed.ok) return
    const trimmed = raw.trim()
    const parsedNumber = Number(trimmed)
    // An empty or non-numeric box writes `null`, NOT a default. The gate then
    // reports its own named budget error ("wall_clock_seconds is required"),
    // which is the truth; substituting a number here would invent a ceiling
    // the operator never chose.
    const next = trimmed === '' || !Number.isFinite(parsedNumber) ? null : parsedNumber
    replacePlan({
      ...parsed.value,
      tasks: readTasks(parsed.value).map((task) =>
        taskIdOf(task) === taskId
          ? {
              ...task,
              budget: {
                ...(task.budget !== null && typeof task.budget === 'object'
                  ? (task.budget as Record<string, unknown>)
                  : {}),
                [field]: next,
              },
            }
          : task,
      ),
    })
  }

  async function handleDispatch() {
    const taskCount = verdict?.task_ids.length ?? 0
    const effective = verdict?.parallelism?.effective ?? EM_DASH
    const confirmed = window.confirm(
      t('partitions.dispatchConfirm', { count: taskCount, effective }),
    )
    if (!confirmed) return

    setSubmitting(true)
    setError(null)
    setResult(null)
    try {
      const outcome = await ratifyPartition(detail.id, {
        partition_json: text,
        outward_consent: consent,
        expected_digest: detail.proposed_digest,
      })
      setResult(
        outcome.idempotent
          ? t('partitions.idempotent')
          : t('partitions.dispatched', { count: outcome.task_ids.length }),
      )
      onRatified()
    } catch (err) {
      // The gate's HTTP status is TRANSLATED here, never re-derived: 403 is
      // the role refusal, 409 is `DigestMismatchError`'s own "this plan was
      // superseded". Anything else surfaces the server's message verbatim.
      if (err instanceof ApiForbiddenError) {
        setError(t('partitions.insufficientRole'))
      } else if (err instanceof ApiError && err.status === 409) {
        setError(`${t('partitions.errorStale')} ${err.message}`)
      } else {
        setError(describeApiError(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  const tasks = parsed.ok ? readTasks(parsed.value) : []

  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <SectionHeader index="01" title={t('partitions.planTitle')} />
        <p className="text-xs break-words text-muted-foreground">{t('partitions.planHint')}</p>
        <textarea
          data-testid="partition-plan-editor"
          aria-label={t('partitions.planLabel')}
          spellCheck={false}
          value={text}
          disabled={submitting}
          onChange={(event) => setText(event.target.value)}
          className="metric-mono min-h-64 w-full rounded-lg border border-input bg-transparent p-2.5 text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30"
        />

        {!parsed.ok && (
          <div
            role="alert"
            data-testid="partition-parse-error"
            className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm break-words text-destructive"
          >
            {t('partitions.parseErrorLead')} {parsed.message}
          </div>
        )}

        {parsed.ok && preview.status === 'loading' && (
          <span role="status" className="text-sm text-muted-foreground">
            {t('partitions.checking')}
          </span>
        )}

        {previewFailed && (
          <div role="alert" className="text-sm break-words text-destructive">
            {t('partitions.previewUnavailable')} {describeApiError(preview.error)}
          </div>
        )}

        {verdict !== null && !verdict.ok && (
          <div
            role="alert"
            data-testid="partition-gate-refusal"
            className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm break-words text-destructive"
          >
            {t('partitions.gateRefusedLead')} {verdict.detail}
          </div>
        )}

        {verdict !== null && verdict.ok && (
          <p data-testid="partition-gate-accepted" className="text-sm text-[var(--color-good)]">
            {t('partitions.gateAccepted')}
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader index="02" title={t('partitions.tasksTitle')} />
        <TaskControls
          tasks={tasks}
          disabled={submitting}
          onDrop={handleDrop}
          onBudgetChange={handleBudgetChange}
        />
        <p className="text-xs break-words text-muted-foreground">{t('partitions.wallClockHelp')}</p>
        <p className="text-xs break-words text-muted-foreground">{t('partitions.costHelp')}</p>
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader index="03" title={t('partitions.outwardTitle')} />
        <OutwardConsent
          actions={verdict?.outward_actions ?? []}
          consent={consent}
          disabled={submitting}
          onChange={setConsent}
        />
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader index="04" title={t('partitions.parallelismTitle')} />
        <ParallelismReadout parallelism={verdict?.parallelism ?? null} />
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{t('partitions.planCostLabel')}:</span>
          <span className="metric-mono text-foreground">
            {verdict?.total_cost_usd == null ? EM_DASH : `$${verdict.total_cost_usd.toFixed(2)}`}
          </span>
        </div>
        {verdict !== null && verdict.waves.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs text-muted-foreground">{t('partitions.wavesLabel')}</span>
            <div className="flex flex-wrap gap-1.5">
              {verdict.waves.map((wave, index) => (
                <Badge key={`wave-${index}`} variant="secondary" className="metric-mono">
                  {t('partitions.waveNumber', { index: index + 1 })}: {wave.join(', ')}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </section>

      <div className="flex flex-col gap-2">
        <Button
          type="button"
          className="touch-target h-11 w-full"
          disabled={!canDispatch}
          aria-label={t('partitions.dispatchAriaLabel', { id: detail.id })}
          onClick={() => {
            void handleDispatch()
          }}
        >
          {submitting ? t('common.processing') : t('partitions.dispatch')}
        </Button>
        {submitting && (
          <span role="status" className="text-sm text-muted-foreground">
            {t('common.processing')}
          </span>
        )}
        {!canDispatch && !submitting && (
          <span className="text-xs break-words text-muted-foreground">
            {t('partitions.dispatchBlocked')}
          </span>
        )}
        {result && (
          <p role="status" className="text-sm break-words text-[var(--color-good)]">
            {result}
          </p>
        )}
        {error && (
          <div role="alert" className="text-sm break-words text-destructive">
            {error}
          </div>
        )}
      </div>
    </div>
  )
}

interface RatifyDrawerProps {
  partitionId: string
  onClose: () => void
  onRatified: () => void
}

/** Loads the partition, then hands a resolved detail to `RatifyForm`. Uses the
 * shared `Drawer` so it gets the backdrop, Escape-to-close and its own scroll
 * container — which is also what keeps a tall JSON editor from scrolling the
 * page body on a phone. Widened past the default `max-w-lg`: a JSON document
 * in a 512px column is unreadable on a laptop, and the drawer is full-width
 * below `sm:` regardless. */
function RatifyDrawer({ partitionId, onClose, onRatified }: RatifyDrawerProps) {
  const t = useT()
  const state = useAsyncData(() => fetchPartition(partitionId), [partitionId])
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  return (
    <Drawer
      title={t('partitions.drawerTitle')}
      ariaLabel={t('partitions.drawerAriaLabel', { id: partitionId })}
      closeLabel={t('partitions.closeAriaLabel')}
      onClose={onClose}
      className="max-w-full sm:max-w-3xl"
    >
      <p className="metric-mono text-xs break-all text-muted-foreground">{partitionId}</p>
      {isForbidden ? (
        <div
          data-testid="partitions-detail-forbidden"
          className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
        >
          {t('partitions.insufficientRole')}
        </div>
      ) : (
        <AsyncSection state={state} isEmpty={() => false}>
          {(detail) =>
            detail.status === RATIFIABLE_STATUS ? (
              <RatifyForm detail={detail} onRatified={onRatified} />
            ) : (
              <div
                data-testid="partitions-not-ratifiable"
                className="rounded-lg border border-border bg-muted/50 p-3 text-sm break-words text-muted-foreground"
              >
                {t('partitions.notRatifiable', { status: detail.status })}
              </div>
            )
          }
        </AsyncSection>
      )}
    </Drawer>
  )
}

const STATUS_BADGE_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  proposed: 'outline',
  ratified: 'default',
  dispatching: 'default',
  completed: 'secondary',
  failed: 'destructive',
  vetoed: 'destructive',
  expired: 'secondary',
}

/**
 * Partitions — `GET /v1/partitions` (tenant-scoped, `run` floor), with the
 * ratification flow behind the shared `Drawer`.
 *
 * Visible to any token like every other built-in tab, but the list endpoint
 * itself needs `run` (a partition document carries every task's full prompt),
 * so a bare `read` token gets a graceful message rendered BEFORE
 * `AsyncSection` — the same carve-out `ApprovalsView` uses. The Review control
 * only renders for `useRole().can('approve')`: a `run` token sees the list,
 * read-only.
 *
 * Deliberately not polled. An operator reading a plan they are about to
 * ratify must not have it move under them; the list refreshes when a
 * ratification actually changes something.
 */
export function PartitionsView() {
  const t = useT()
  const { can } = useRole()
  const canRatify = can('approve')
  const [refreshKey, setRefreshKey] = useState(0)
  const [openId, setOpenId] = useState<string | null>(null)
  const state = useAsyncData(() => fetchPartitions(), [refreshKey])
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  function handleRatified() {
    setRefreshKey((key) => key + 1)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('nav.partitions')}</CardTitle>
        <CardDescription>
          {canRatify ? t('partitions.description') : t('partitions.descriptionReadOnly')}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isForbidden ? (
          <div
            data-testid="partitions-forbidden"
            className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
          >
            {t('partitions.forbidden')}
          </div>
        ) : (
          <AsyncSection state={state} isEmpty={() => false}>
            {(data) =>
              data.length === 0 ? (
                <EmptyState
                  data-testid="partitions-empty"
                  icon={<GitBranch className="size-4" />}
                  title={t('partitions.emptyTitle')}
                  body={t('partitions.emptyBody')}
                />
              ) : (
                <ul className="flex flex-col gap-2">
                  {data.map((partition: PartitionSummary) => (
                    <li
                      key={partition.id}
                      data-testid={`partition-row-${partition.id}`}
                      className={cn(
                        'flex flex-col gap-2 rounded-lg border border-border p-3 text-sm',
                        partition.status === RATIFIABLE_STATUS &&
                          'border-l-4 border-l-[var(--color-warn)]',
                      )}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="metric-mono min-w-0 break-all">{partition.id}</span>
                        <Badge variant={STATUS_BADGE_VARIANT[partition.status] ?? 'secondary'}>
                          {partition.status}
                        </Badge>
                      </div>
                      <span className="text-xs break-words text-muted-foreground">
                        {t('partitions.sourceLabel')}:{' '}
                        {partition.source_kind === null
                          ? EM_DASH
                          : `${partition.source_kind}:${partition.source_ref ?? EM_DASH}`}
                      </span>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="metric-mono text-xs text-muted-foreground">
                          {t('partitions.proposedAgo', { age: formatAge(partition.created_ts) })}
                        </span>
                        {canRatify && partition.status === RATIFIABLE_STATUS && (
                          <Button
                            type="button"
                            size="sm"
                            className="touch-target ml-auto"
                            aria-label={t('partitions.reviewAriaLabel', { id: partition.id })}
                            onClick={() => setOpenId(partition.id)}
                          >
                            {t('partitions.review')}
                          </Button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )
            }
          </AsyncSection>
        )}
      </CardContent>

      {openId !== null && (
        <RatifyDrawer
          partitionId={openId}
          onClose={() => setOpenId(null)}
          onRatified={handleRatified}
        />
      )}
    </Card>
  )
}
