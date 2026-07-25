import { X } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiForbiddenError } from '@/lib/api'
import { useT } from '@/lib/i18n'
import {
  type AgentRoster,
  type AgentUnknownBucket,
  type Lesson,
  type LessonsResponse,
  type SuccessRate,
  type Verdict,
  type VerdictRoleAggregation,
  type VerdictsResponse,
  fetchAgents,
  fetchLessons,
  fetchVerdicts,
} from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'
import { AsyncSection } from './AsyncSection'

/** A wider-than-`limit=50` window for the unfiltered severity fetch (see
 * `hasNonNominalVerdict` below) — the roster can hold more roles than the
 * default recent-verdicts window would otherwise cover; still bounded by
 * the backend's own `le=500` cap on `/v1/verdicts`. This is a "recent
 * activity" signal, not an exhaustive one — same honesty posture as
 * `fetchVerdicts`'s own `limit` contract (never silently unbounded). */
const SEVERITY_VERDICTS_LIMIT = 200

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

const RATE_TONE_CLASS: Record<'good' | 'warn' | 'crit', string> = {
  good: 'text-[var(--color-good)]',
  warn: 'text-[var(--color-warn)]',
  crit: 'text-[var(--color-crit)]',
}

function rateTone(rate: number): 'good' | 'warn' | 'crit' {
  if (rate >= 0.8) return 'good'
  if (rate >= 0.5) return 'warn'
  return 'crit'
}

/** Elapsed time from `iso` to now, as a short "2d"/"3h"/"12m"/"45s" string —
 * same convention as `AutopilotView`/`RunBoardView`'s own copy of this
 * helper. Unparseable/missing input renders as "—", never a fabricated
 * age. */
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

/**
 * "Non-nominal" for the severity stripe/badge: a real, non-`"ACCEPT"`
 * decision string (case-insensitive) counted in a role's recent
 * `decision_counts` — covers `null`/failed decisions (aggregated as the
 * `"unknown"` key by `verdicts_summary`) and any other explicit rejection
 * text (`"MAINTAIN"`, `"REJECT"`, ...). A role with ONLY `"ACCEPT"` recent
 * verdicts (or no verdicts at all) stays nominal — never flagged just for
 * having no data.
 */
function hasNonNominalVerdict(agg: VerdictRoleAggregation | undefined): boolean {
  if (!agg) return false
  return Object.entries(agg.decision_counts).some(
    ([decision, count]) => count > 0 && decision.trim().toUpperCase() !== 'ACCEPT',
  )
}

function isNonNominalDecision(decision: string | null): boolean {
  return decision === null || decision.trim().toUpperCase() !== 'ACCEPT'
}

interface SuccessRateReadoutProps {
  rate: SuccessRate
  roleName: string
}

/** `SuccessRate` is `number | null` — `null` means zero attempts in-window
 * (`_attempt_success_rate` excludes skipped/other from its denominator),
 * rendered as an honest "no attempts yet" instead of a fabricated
 * percentage, mirroring `ModelsView`'s `SuccessRateCell`. */
function SuccessRateReadout({ rate, roleName }: SuccessRateReadoutProps) {
  const t = useT()
  if (rate === null) {
    return (
      <span data-testid={`agent-no-success-rate-${roleName}`} className="text-muted-foreground">
        {t('agents.noAttemptsYet')}
      </span>
    )
  }
  return <span className={RATE_TONE_CLASS[rateTone(rate)]}>{Math.round(rate * 100)}%</span>
}

interface AgentCardProps {
  agent: AgentRoster
  nonNominal: boolean
  selected: boolean
  onSelect: () => void
}

/**
 * One roster card. `attributed: false` -> an honest "no activity attributed
 * yet" state, never a fabricated `0%`/rollup (see `AgentsResponse`'s
 * docstring in `mirador-api.ts`). `display_name`/`title` are role config
 * text, `name` is an identifier — none of it is ever HTML-rendered, only
 * plain JSX interpolation (React auto-escapes), so even a role name/display
 * name that happens to contain markup can never execute.
 */
function AgentCard({ agent, nonNominal, selected, onSelect }: AgentCardProps) {
  const t = useT()
  const label = agent.display_name ?? agent.name

  return (
    <button
      type="button"
      data-testid={`agent-card-${agent.name}`}
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        'flex flex-col gap-2 rounded-lg border border-border bg-card p-3 text-left text-sm transition-colors hover:bg-muted/50',
        selected && 'ring-2 ring-ring',
        nonNominal && 'border-l-4 border-l-[var(--color-crit)]',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{label}</span>
          {agent.title && <span className="truncate text-xs text-muted-foreground">{agent.title}</span>}
        </div>
        {nonNominal && (
          <Badge variant="destructive" data-testid={`agent-nonnominal-${agent.name}`}>
            {t('agents.attentionBadge')}
          </Badge>
        )}
      </div>

      {!agent.attributed ? (
        <p data-testid={`agent-no-activity-${agent.name}`} className="text-xs text-muted-foreground">
          {t('agents.noActivityYet')}
        </p>
      ) : (
        <div className="metric-mono grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <span className="text-muted-foreground">{t('agents.costLabel')}</span>
          <span>{formatCost(agent.cost_usd)}</span>
          <span className="text-muted-foreground">{t('agents.runsLabel')}</span>
          <span>{agent.run_count.toLocaleString('en-US')}</span>
          <span className="text-muted-foreground">{t('agents.stepsLabel')}</span>
          <span>{agent.step_count.toLocaleString('en-US')}</span>
          <span className="text-muted-foreground">{t('agents.tokensLabel')}</span>
          <span>
            {formatTokens(agent.input_tokens)} / {formatTokens(agent.output_tokens)}
          </span>
          <span className="text-muted-foreground">{t('agents.lastActiveLabel')}</span>
          <span>{formatAge(agent.last_active)}</span>
          <span className="text-muted-foreground">{t('agents.successRateLabel')}</span>
          <SuccessRateReadout rate={agent.success_rate} roleName={agent.name} />
        </div>
      )}
    </button>
  )
}

/**
 * The top-level `"unknown"` (NULL-role) bucket — historical/unattributed
 * activity, kept visually distinct (dashed border) from the real roster so
 * it never reads as "just another agent". An honest "nothing unattributed"
 * empty state when `step_count === 0`, never a zeroed-out card that looks
 * like a loading glitch.
 */
function UnknownBucketCard({ unknown }: { unknown: AgentUnknownBucket }) {
  const t = useT()

  return (
    <Card data-testid="agents-unknown-bucket" className="border-dashed">
      <CardHeader>
        <CardTitle className="text-sm">{t('agents.unknownTitle')}</CardTitle>
        <CardDescription>{t('agents.unknownDescription')}</CardDescription>
      </CardHeader>
      <CardContent>
        {unknown.step_count === 0 ? (
          <p data-testid="agents-unknown-empty" className="text-sm text-muted-foreground">
            {t('agents.unknownEmpty')}
          </p>
        ) : (
          <div className="metric-mono grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
            <div className="flex flex-col">
              <span className="text-xs text-muted-foreground">{t('agents.costLabel')}</span>
              <span>{formatCost(unknown.cost_usd)}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-xs text-muted-foreground">{t('agents.runsLabel')}</span>
              <span>{unknown.run_count.toLocaleString('en-US')}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-xs text-muted-foreground">{t('agents.stepsLabel')}</span>
              <span>{unknown.step_count.toLocaleString('en-US')}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-xs text-muted-foreground">{t('agents.tokensLabel')}</span>
              <span>
                {formatTokens(unknown.input_tokens)} / {formatTokens(unknown.output_tokens)}
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** A lesson's `text`/`category` are LLM-distilled free text — UNTRUSTED,
 * rendered via plain JSX interpolation only (see `Lesson`'s docstring in
 * `mirador-api.ts`). `validated` is a raw SQLite 0/1 int, treated as a
 * boolean via truthiness. */
function LessonItem({ lesson }: { lesson: Lesson }) {
  const t = useT()
  return (
    <li
      data-testid={`agent-lesson-${lesson.id}`}
      className="flex flex-col gap-1 rounded-lg border border-border p-3 text-sm"
    >
      <p className="break-words whitespace-pre-wrap">{lesson.text}</p>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {lesson.category && <Badge variant="outline">{lesson.category}</Badge>}
        <Badge variant={lesson.validated ? 'default' : 'secondary'}>
          {lesson.validated ? t('agents.validated') : t('agents.candidate')}
        </Badge>
        {lesson.score != null && <span>{t('agents.scoreLabel', { score: lesson.score.toFixed(2) })}</span>}
      </div>
    </li>
  )
}

/** A verdict's `summary` is caller-supplied free text — UNTRUSTED, same
 * caveat as `LessonItem` above. Severity stripe mirrors the roster card's
 * (`isNonNominalDecision`). */
function VerdictItem({ verdict }: { verdict: Verdict }) {
  const t = useT()
  const nonNominal = isNonNominalDecision(verdict.decision)
  return (
    <li
      data-testid={`agent-verdict-${verdict.id}`}
      className={cn(
        'flex flex-col gap-1 rounded-lg border border-border p-3 text-sm',
        nonNominal && 'border-l-4 border-l-[var(--color-crit)]',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{verdict.kind ?? t('agents.unknownKind')}</Badge>
        <Badge variant={nonNominal ? 'destructive' : 'default'}>{verdict.decision ?? t('agents.noDecision')}</Badge>
        {verdict.confidence != null && (
          <span className="text-xs text-muted-foreground">
            {t('agents.confidenceLabel', { confidence: String(Math.round(verdict.confidence * 100)) })}
          </span>
        )}
      </div>
      {verdict.summary && <p className="break-words whitespace-pre-wrap text-muted-foreground">{verdict.summary}</p>}
    </li>
  )
}

interface AgentDetailPanelProps {
  role: string | null
  displayName: string | null
  onClose: () => void
}

/**
 * Per-role drill-down — a right-side drawer (mirrors `RunDetailPanel`'s
 * layout convention) fetching `GET /v1/lessons?role=` and
 * `GET /v1/verdicts?role=` scoped to the selected role, opened by clicking
 * an `AgentCard`. Renders nothing when `role` is `null`, so `AgentsView` can
 * mount it unconditionally.
 */
function AgentDetailPanel({ role, displayName, onClose }: AgentDetailPanelProps) {
  const t = useT()
  const lessonsState = useAsyncData<LessonsResponse>(
    () => (role ? fetchLessons(role) : Promise.resolve({ lessons: [], by_role: {} })),
    [role],
  )
  const verdictsState = useAsyncData<VerdictsResponse>(
    () => (role ? fetchVerdicts(role) : Promise.resolve({ verdicts: [], by_role: {} })),
    [role],
  )

  if (role === null) return null

  const lessonsForbidden = lessonsState.status === 'error' && lessonsState.error instanceof ApiForbiddenError
  const verdictsForbidden = verdictsState.status === 'error' && verdictsState.error instanceof ApiForbiddenError
  const title = displayName ?? role

  return (
    <>
      <div
        data-testid="agent-detail-backdrop"
        aria-hidden="true"
        className="fixed inset-0 z-40 bg-black/50"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-label={t('agents.detailAriaLabel', { name: title })}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col gap-4 overflow-y-auto border-l border-border bg-card p-4 shadow-xl sm:p-6"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={t('agents.closeAriaLabel')}
            onClick={onClose}
          >
            <X className="size-4" />
          </Button>
        </div>

        <div>
          <h3 className="mb-2 text-sm font-semibold">{t('agents.lessonsTitle')}</h3>
          {lessonsForbidden ? (
            <div
              role="alert"
              data-testid="agent-lessons-forbidden"
              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              {t('agents.forbidden')}
            </div>
          ) : (
            <AsyncSection
              state={lessonsState}
              isEmpty={(data) => data.lessons.length === 0}
              emptyMessage={t('agents.noLessons')}
            >
              {(data) => (
                <ul className="flex flex-col gap-2">
                  {data.lessons.map((lesson) => (
                    <LessonItem key={lesson.id} lesson={lesson} />
                  ))}
                </ul>
              )}
            </AsyncSection>
          )}
        </div>

        <div>
          <h3 className="mb-2 text-sm font-semibold">{t('agents.verdictsTitle')}</h3>
          {verdictsForbidden ? (
            <div
              role="alert"
              data-testid="agent-verdicts-forbidden"
              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              {t('agents.forbidden')}
            </div>
          ) : (
            <AsyncSection
              state={verdictsState}
              isEmpty={(data) => data.verdicts.length === 0}
              emptyMessage={t('agents.noVerdicts')}
            >
              {(data) => (
                <ul className="flex flex-col gap-2">
                  {data.verdicts.map((verdict) => (
                    <VerdictItem key={verdict.id} verdict={verdict} />
                  ))}
                </ul>
              )}
            </AsyncSection>
          )}
        </div>
      </div>
    </>
  )
}

/**
 * Mirador "Agents" view — the frontend for the Mirador Agent Panels backend
 * sprint: `GET /v1/agents` (the full role roster left-joined with real
 * per-role activity), `GET /v1/lessons`, `GET /v1/verdicts`. Read-only for
 * every token (all three endpoints gate at `read`).
 *
 * Honesty, enforced end to end:
 * - `attributed: false` -> "No activity attributed yet", never a fabricated
 *   `0%` success rate (see `AgentCard`).
 * - `success_rate: null` -> "No attempts yet", never rendered as a number
 *   (see `SuccessRateReadout`) — this can happen even for an attributed
 *   role (zero *attempted* outcomes in a skipped-only window).
 * - The NULL-role `unknown` bucket is a visually distinct card, never
 *   folded into the roster grid, with its own honest empty state and the
 *   backend's `note` rendered verbatim (see `UnknownBucketCard`).
 * - No latency figure is ever shown — the backend never returns one.
 * - A severity stripe/badge appears ONLY on a role with a recent
 *   non-`"ACCEPT"` verdict (`hasNonNominalVerdict`, sourced from an
 *   unfiltered `GET /v1/verdicts`' `by_role` aggregation) — a nominal role,
 *   or one with no verdict history at all, stays visually clean.
 * - Every free-text field (lesson `text`, verdict `summary`, role
 *   `display_name`/`title`) renders via plain JSX interpolation only —
 *   never `dangerouslySetInnerHTML`.
 */
export function AgentsView() {
  const t = useT()
  const agentsState = useAsyncData(() => fetchAgents(), [])
  const verdictsState = useAsyncData(() => fetchVerdicts(undefined, SEVERITY_VERDICTS_LIMIT), [])
  const [selectedRole, setSelectedRole] = useState<string | null>(null)

  const isAgentsForbidden = agentsState.status === 'error' && agentsState.error instanceof ApiForbiddenError
  const isVerdictsForbidden = verdictsState.status === 'error' && verdictsState.error instanceof ApiForbiddenError
  const byRole = verdictsState.status === 'success' ? verdictsState.data.by_role : {}

  const selectedAgent =
    agentsState.status === 'success'
      ? (agentsState.data.agents.find((agent) => agent.name === selectedRole) ?? null)
      : null

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('agents.title')}</CardTitle>
          <CardDescription>{t('agents.description')}</CardDescription>
        </CardHeader>
        <CardContent>
          {isAgentsForbidden ? (
            <div
              role="alert"
              data-testid="agents-forbidden"
              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              {t('agents.forbidden')}
            </div>
          ) : (
            <AsyncSection
              state={agentsState}
              isEmpty={(data) => data.agents.length === 0}
              emptyMessage={t('agents.noRoster')}
            >
              {(data) => (
                <div className="flex flex-col gap-4">
                  <p data-testid="agents-note" className="text-xs text-muted-foreground">
                    {data.note}
                  </p>

                  <div
                    data-testid="agents-roster-grid"
                    className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
                  >
                    {data.agents.map((agent) => (
                      <AgentCard
                        key={agent.name}
                        agent={agent}
                        nonNominal={hasNonNominalVerdict(byRole[agent.name])}
                        selected={selectedRole === agent.name}
                        onSelect={() => setSelectedRole(agent.name)}
                      />
                    ))}
                  </div>

                  <UnknownBucketCard unknown={data.unknown} />
                </div>
              )}
            </AsyncSection>
          )}
        </CardContent>
      </Card>

      {!isAgentsForbidden && isVerdictsForbidden && (
        <div
          role="alert"
          data-testid="agents-verdicts-forbidden"
          className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
        >
          {t('agents.verdictsForbidden')}
        </div>
      )}

      <AgentDetailPanel
        role={selectedRole}
        displayName={selectedAgent?.display_name ?? null}
        onClose={() => setSelectedRole(null)}
      />
    </div>
  )
}
