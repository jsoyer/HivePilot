import { AlertTriangle } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Drawer } from '@/components/ui/drawer'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { ApiForbiddenError } from '@/lib/api'
import { EM_DASH, formatAge } from '@/lib/format-time'
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
  fetchAgentsLive,
  fetchLessons,
  fetchVerdicts,
  sendAgentMessage,
} from '@/lib/pollen-api'
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

/** Below this, a role's success rate is a problem worth surfacing at the
 * top of the page. Matches `rateTone`'s `crit` threshold. */
const CRIT_RATE = 0.5
/** Below this, a role's success rate is worth a warning tone but not the
 * attention band. */
const WARN_RATE = 0.8

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

type Tone = 'good' | 'warn' | 'crit'

const TONE_TEXT: Record<Tone, string> = {
  good: 'text-[var(--color-good)]',
  warn: 'text-[var(--color-warn)]',
  crit: 'text-[var(--color-crit)]',
}

const TONE_STRIPE: Record<Tone, string> = {
  good: '',
  warn: 'border-l-4 border-l-[var(--color-warn)]',
  crit: 'border-l-4 border-l-[var(--color-crit)]',
}

function rateTone(rate: number): Tone {
  if (rate >= WARN_RATE) return 'good'
  if (rate >= CRIT_RATE) return 'warn'
  return 'crit'
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

/**
 * The single "how bad is this role" judgement, computed once and reused by
 * the attention band, the row stripe and the sort order — so the three can
 * never disagree.
 *
 * `crit` means a human should look now: a recent non-`ACCEPT` verdict, or a
 * success rate under 50%. `warn` is a rate under 80%. Everything else,
 * INCLUDING a role with no data at all, is `good` — a role is never flagged
 * for being quiet.
 */
function roleTone(agent: AgentRoster, nonNominalVerdict: boolean): Tone {
  if (nonNominalVerdict) return 'crit'
  if (agent.success_rate === null) return 'good'
  return rateTone(agent.success_rate)
}

const TONE_RANK: Record<Tone, number> = { crit: 0, warn: 1, good: 2 }

interface SuccessRateCellProps {
  rate: SuccessRate
  roleName: string
  attributed: boolean
}

/**
 * `SuccessRate` is `number | null` — `null` means zero attempts in-window
 * (`_attempt_success_rate` excludes skipped/other from its denominator),
 * rendered as an em-dash with an explanatory title instead of a fabricated
 * percentage.
 *
 * The number itself carries the weight: a below-threshold rate is bold and
 * tinted, a healthy one is plain. In the previous card grid a lone 20% sat
 * among fourteen 100%s in identical type, which is exactly how the single
 * most important signal on the page went unnoticed.
 */
function SuccessRateCell({ rate, roleName, attributed }: SuccessRateCellProps) {
  const t = useT()
  if (rate === null) {
    return (
      <span
        data-testid={`agent-no-success-rate-${roleName}`}
        title={attributed ? t('agents.noAttemptsYet') : t('agents.noActivityYet')}
        className="text-muted-foreground"
      >
        {EM_DASH}
      </span>
    )
  }
  const tone = rateTone(rate)
  return (
    <span
      data-testid={`agent-success-rate-${roleName}`}
      className={cn('metric-mono', TONE_TEXT[tone], tone === 'good' ? 'font-normal' : 'font-semibold')}
    >
      {Math.round(rate * 100)}%
    </span>
  )
}

interface CostCellProps {
  agent: AgentRoster
  max: number
}

/**
 * Cost, with magnitude encoded as weight AND as a proportional bar.
 *
 * The review's complaint was that `$7.259` and `$0.000` rendered in
 * identical type, so the roster's one expensive role was invisible. The bar
 * is drawn relative to the biggest spender on screen, and the numeral gets
 * full foreground weight once a role accounts for a meaningful share.
 *
 * An unattributed role has no cost to report at all and renders an em-dash —
 * never `$0.000`, which would read as a measured zero.
 */
function CostCell({ agent, max }: CostCellProps) {
  if (!agent.attributed) {
    return <span className="text-muted-foreground">{EM_DASH}</span>
  }
  const share = max > 0 ? agent.cost_usd / max : 0
  const prominent = share >= 0.25
  return (
    <div className="flex flex-col items-end gap-1">
      <span
        data-testid={`agent-cost-${agent.name}`}
        className={cn(
          'metric-mono',
          prominent ? 'font-semibold text-foreground' : 'text-muted-foreground',
        )}
      >
        {formatCost(agent.cost_usd)}
      </span>
      <span aria-hidden="true" className="h-1 w-16 overflow-hidden rounded-full bg-muted">
        <span
          data-testid={`agent-cost-bar-${agent.name}`}
          className="block h-full rounded-full bg-[var(--color-primary)]"
          style={{ width: `${Math.round(Math.max(0, Math.min(1, share)) * 100)}%` }}
        />
      </span>
    </div>
  )
}

interface RosterRow {
  agent: AgentRoster
  tone: Tone
  nonNominalVerdict: boolean
}

interface AttentionBandProps {
  rows: RosterRow[]
  onSelect: (name: string) => void
}

/**
 * The answer to "is anything wrong?", above the roster, before an operator
 * has to read fifteen rows to find out.
 *
 * Only roles the `roleTone` judgement calls `crit` appear here. When
 * nothing is wrong this component renders a quiet all-clear line rather
 * than nothing at all — the absence of a band is ambiguous, an explicit
 * "all clear" is not.
 */
function AttentionBand({ rows, onSelect }: AttentionBandProps) {
  const t = useT()
  const critical = rows.filter((row) => row.tone === 'crit')

  if (critical.length === 0) {
    return (
      <p data-testid="agents-all-clear" className="text-sm text-[var(--color-good)]">
        {t('agents.allClear')}
      </p>
    )
  }

  return (
    <div
      data-testid="agents-attention-band"
      className="flex flex-col gap-2 rounded-lg border border-[var(--color-crit)]/40 bg-[var(--color-crit)]/5 p-3"
    >
      <div className="flex items-center gap-2">
        <AlertTriangle aria-hidden="true" className="size-4 text-[var(--color-crit)]" />
        <span className="text-sm font-medium">{t('agents.attentionTitle', { count: critical.length })}</span>
      </div>
      <ul className="flex flex-col gap-1">
        {critical.map(({ agent, nonNominalVerdict }) => (
          <li key={agent.name}>
            <button
              type="button"
              data-testid={`agents-attention-${agent.name}`}
              onClick={() => onSelect(agent.name)}
              className="flex w-full flex-wrap items-center gap-x-2 gap-y-0.5 rounded px-1 py-0.5 text-left text-sm hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <AgentAvatar roleName={agent.name} label={agent.display_name ?? agent.name} />
              <span className="font-medium">{agent.display_name ?? agent.name}</span>
              <span className="text-muted-foreground">
                {nonNominalVerdict
                  ? t('agents.reasonVerdict')
                  : t('agents.reasonLowSuccess', {
                      rate: String(Math.round((agent.success_rate ?? 0) * 100)),
                    })}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

interface RosterTableProps {
  rows: RosterRow[]
  maxCost: number
  selected: string | null
  onSelect: (name: string) => void
}

/**
 * The roster as a table, not fifteen identical cards.
 *
 * Cards make values incomparable: each one is its own `label: value` list,
 * so reading "which role costs the most" means reading every card. A table
 * puts the same figure in the same column at the same x-position, which is
 * what makes a $7.259 stand out from a $0.000 without any decoration at
 * all — the weight and the bar then reinforce it.
 *
 * Sorted by severity first, then by cost descending: the two questions an
 * operator actually opens this page with, in that order.
 *
 * The table scrolls inside its own container (see `ui/table.tsx`), so a
 * narrow viewport never scrolls the page body sideways.
 */
function RosterTable({ rows, maxCost, selected, onSelect }: RosterTableProps) {
  const t = useT()

  return (
    <Table scrollLabel={t('agents.tableScrollLabel')}>
      <TableHeader>
        <TableRow>
          <TableHead>{t('agents.colRole')}</TableHead>
          <TableHead className="text-right">{t('agents.successRateLabel')}</TableHead>
          <TableHead className="text-right">{t('agents.costLabel')}</TableHead>
          <TableHead className="text-right">{t('agents.runsLabel')}</TableHead>
          <TableHead className="text-right">{t('agents.stepsLabel')}</TableHead>
          <TableHead className="text-right">{t('agents.tokensLabel')}</TableHead>
          <TableHead className="text-right">{t('agents.lastActiveLabel')}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map(({ agent, tone }) => (
          <TableRow
            key={agent.name}
            data-testid={`agent-row-${agent.name}`}
            role="button"
            tabIndex={0}
            aria-label={t('agents.rowAriaLabel', { name: agent.display_name ?? agent.name })}
            aria-pressed={selected === agent.name}
            onClick={() => onSelect(agent.name)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onSelect(agent.name)
              }
            }}
            className={cn(
              'cursor-pointer focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
              TONE_STRIPE[tone],
              selected === agent.name && 'bg-muted/60',
            )}
          >
            <TableCell>
              <div className="flex min-w-0 items-center gap-2">
                <AgentAvatar roleName={agent.name} label={agent.display_name ?? agent.name} />
                <div className="flex min-w-0 flex-col">
                <span className="truncate font-medium">{agent.display_name ?? agent.name}</span>
                {agent.title && (
                  <span className="truncate text-xs text-muted-foreground">{agent.title}</span>
                )}
                {!agent.attributed && (
                  <span
                    data-testid={`agent-no-activity-${agent.name}`}
                    className="text-xs text-muted-foreground"
                  >
                    {t('agents.noActivityYet')}
                  </span>
                )}
                </div>
              </div>
            </TableCell>
            <TableCell className="text-right">
              <SuccessRateCell
                rate={agent.success_rate}
                roleName={agent.name}
                attributed={agent.attributed}
              />
            </TableCell>
            <TableCell className="text-right">
              <CostCell agent={agent} max={maxCost} />
            </TableCell>
            <TableCell className="metric-mono text-right text-muted-foreground">
              {agent.attributed ? agent.run_count.toLocaleString('en-US') : EM_DASH}
            </TableCell>
            <TableCell className="metric-mono text-right text-muted-foreground">
              {agent.attributed ? agent.step_count.toLocaleString('en-US') : EM_DASH}
            </TableCell>
            <TableCell className="metric-mono text-right whitespace-nowrap text-muted-foreground">
              {agent.attributed
                ? `${formatTokens(agent.input_tokens)} / ${formatTokens(agent.output_tokens)}`
                : EM_DASH}
            </TableCell>
            <TableCell className="metric-mono text-right text-muted-foreground">
              {formatAge(agent.last_active)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** Avatar tints. Chosen to stay legible on both grounds and to read as
 * distinct from the semantic colours used elsewhere in this view (amber for
 * attention, red for failure) — an agent's identity must never look like a
 * status. */
const AVATAR_TONES = [
  'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  'bg-violet-500/15 text-violet-700 dark:text-violet-300',
  'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  'bg-teal-500/15 text-teal-700 dark:text-teal-300',
  'bg-fuchsia-500/15 text-fuchsia-700 dark:text-fuchsia-300',
  'bg-indigo-500/15 text-indigo-700 dark:text-indigo-300',
]

/** Deterministic tint from the ROLE name, not the display name.
 *
 * HivePilot is a generic engine: personas, their names and their count are
 * tenant configuration, so there can be no built-in name-to-icon table. A
 * hash gives every org stable per-agent identity without the engine knowing
 * anything about who its agents are. Keying on the role name (rather than
 * the display name) keeps the colour stable when a tenant renames a persona. */
function avatarTone(roleName: string): string {
  let hash = 0
  for (let index = 0; index < roleName.length; index += 1) {
    hash = (hash * 31 + roleName.charCodeAt(index)) | 0
  }
  return AVATAR_TONES[Math.abs(hash) % AVATAR_TONES.length]
}

/** `aria-hidden` on purpose: the name it decorates is rendered right beside
 * it, so announcing the initial too would just stutter for a screen reader. */
function AgentAvatar({ roleName, label }: { roleName: string; label: string }) {
  // Spread rather than `charAt`, so an accented or non-Latin first character
  // (or an emoji) survives instead of being split mid-codepoint.
  const initial = [...label.trim()][0]?.toUpperCase() ?? '?'
  return (
    <span
      aria-hidden="true"
      data-testid={`agent-avatar-${roleName}`}
      className={`inline-flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${avatarTone(roleName)}`}
    >
      {initial}
    </span>
  )
}

/**
 * The top-level `"unknown"` (NULL-role) bucket, kept visually distinct
 * (dashed border) from the real roster so it never reads as "just another
 * agent".
 *
 * The backend's `unknown.note` is deliberately NOT rendered: it explains an
 * implementation detail (which sprint added the `steps.role` column) to
 * whoever reads the API, not something an operator can act on.
 *
 * **The breakdown is the point of this card.** It used to show one number
 * described as history recorded before per-role attribution existed. On real
 * data that description was wrong for every row: the overwhelming majority
 * were `shell` steps that cannot have a role at all, and buried inside was a
 * much smaller set of model invocations that genuinely should have been
 * attributed — carrying real spend that no per-agent figure accounted for.
 * A single total made the harmless and the defective look identical, so the
 * three causes are now named and counted separately.
 */
function UnknownBucketCard({ unknown }: { unknown: AgentUnknownBucket }) {
  const t = useT()

  if (unknown.step_count === 0) {
    return null
  }

  const { no_model: noModel, skipped, attribution_gap: gap } = unknown.breakdown
  const causes = [
    { key: 'noModel', label: t('agents.unknown.noModel'), hint: t('agents.unknown.noModelHint'), part: noModel },
    { key: 'skipped', label: t('agents.unknown.skipped'), hint: t('agents.unknown.skippedHint'), part: skipped },
    {
      key: 'attributionGap',
      label: t('agents.unknown.attributionGap'),
      hint: t('agents.unknown.attributionGapHint'),
      part: gap,
      isDefect: true,
    },
  ]

  return (
    <div data-testid="agents-unknown-bucket" className="rounded-lg border border-dashed border-border p-3">
      <p className="mb-2 text-sm font-medium">{t('agents.unknownTitle')}</p>
      <p className="mb-3 max-w-prose text-xs text-muted-foreground">{t('agents.unknownDescription')}</p>

      <ul className="mb-3 flex flex-col gap-2">
        {causes.map((cause) => (
          <li
            key={cause.key}
            data-testid={`agents-unknown-cause-${cause.key}`}
            className={`flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 rounded-md px-2 py-1.5 ${
              // Only the defect gets emphasis, and only when it is actually
              // happening — a permanently amber row would stop meaning
              // anything.
              cause.isDefect && cause.part.step_count > 0
                ? 'bg-amber-500/10 text-amber-700 dark:text-amber-500'
                : 'text-muted-foreground'
            }`}
          >
            <span className="text-sm font-medium">{cause.label}</span>
            <span className="metric-mono text-sm tabular-nums">
              {t('agents.unknown.stepsSuffix', { count: cause.part.step_count.toLocaleString('en-US') })}
              {cause.part.cost_usd > 0 && ` · ${formatCost(cause.part.cost_usd)}`}
            </span>
            <span className="w-full text-xs opacity-80">{cause.hint}</span>
          </li>
        ))}
      </ul>

      {/* Named in money, because that is what makes it worth acting on. */}
      {gap.cost_usd > 0 && (
        <p
          data-testid="agents-unknown-gap-cost"
          className="mb-3 max-w-prose text-xs font-medium text-amber-700 dark:text-amber-500"
        >
          {t('agents.unknown.gapCost', { cost: formatCost(gap.cost_usd) })}
        </p>
      )}
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
    </div>
  )
}

/** A lesson's `text`/`category` are LLM-distilled free text — UNTRUSTED,
 * rendered via plain JSX interpolation only (see `Lesson`'s docstring in
 * `pollen-api.ts`). `validated` is a raw SQLite 0/1 int, treated as a
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
 * caveat as `LessonItem` above. Severity stripe mirrors the roster row's
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
 * Per-role drill-down — a right-side drawer fetching `GET /v1/lessons?role=`
 * and `GET /v1/verdicts?role=` scoped to the selected role, opened by
 * activating a roster row. Renders nothing when `role` is `null`, so
 * `AgentsView` can mount it unconditionally.
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
    <Drawer
      title={title}
      ariaLabel={t('agents.detailAriaLabel', { name: title })}
      closeLabel={t('agents.closeAriaLabel')}
      onClose={onClose}
    >
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
    </Drawer>
  )
}

/**
 * Agents — `GET /v1/agents` (the full role roster left-joined with real
 * per-role activity), `GET /v1/lessons`, `GET /v1/verdicts`. Read-only for
 * every token (all three endpoints gate at `read`).
 *
 * What the operator review asked for, and where it lives:
 *  - the worst role first, not buried among fourteen healthy ones ->
 *    `AttentionBand` + the severity-then-cost sort in `rows`;
 *  - cost that reads at a glance -> `CostCell`'s weight + proportional bar;
 *  - comparable figures -> a table, not fifteen identical cards.
 *
 * `AgentsResponse.note` is deliberately NOT rendered. It is a five-line
 * engineering rationale about when the `steps.role` column was introduced
 * and why no latency figure exists — a changelog entry, useful to whoever
 * reads the API, useless to whoever operates it. The two operator-relevant
 * consequences are expressed directly instead: an unattributed role says
 * "no activity attributed yet", and no latency column exists at all.
 *
 * Honesty, enforced end to end:
 *  - `attributed: false` -> every metric cell renders an em-dash, never a
 *    fabricated `$0.000`/`0%`.
 *  - `success_rate: null` -> an em-dash with an explanatory title, never a
 *    number (this happens even for an attributed role, in a skipped-only
 *    window).
 *  - a role is never flagged for having NO data — quiet is not a fault.
 *  - every free-text field (lesson `text`, verdict `summary`, role
 *    `display_name`/`title`) renders via plain JSX interpolation only.
 */

/** Live state per role, and a line to talk to one.
 *
 * `GET /v1/agents` answers what a role has DONE; this answers what it is
 * doing NOW. The two are deliberately separate components: historical
 * activity is always available, a live surface may not be configured at all,
 * and collapsing them would make an unconfigured deployment look broken.
 *
 * When `configured` is false the REASON is rendered, not a row of `unknown`
 * badges. An operator seeing every agent unknown without an explanation reads
 * a bug in the dashboard; seeing "no agent surface configured" reads a
 * setting they can change.
 */
function LiveAgentsPanel() {
  const t = useT()
  const live = useAsyncData(() => fetchAgentsLive(), [])
  const [role, setRole] = useState('')
  const [text, setText] = useState('')
  const [outcome, setOutcome] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (live.status !== 'success') return null
  const { configured, detail, agents } = live.data

  async function send(event: React.FormEvent) {
    event.preventDefault()
    if (!role || !text.trim() || busy) return
    setBusy(true)
    setOutcome(null)
    try {
      const res = await sendAgentMessage(role, text)
      // "dispatched", never "delivered": the API cannot know the agent read it.
      setOutcome(res.dispatched ? t('agents.live.dispatched') : (res.detail ?? t('agents.live.refused')))
      if (res.dispatched) setText('')
    } catch {
      setOutcome(t('agents.live.refused'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card data-testid="live-agents">
      <CardHeader>
        <CardTitle>{t('agents.live.title')}</CardTitle>
        {!configured && <CardDescription>{detail}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-2">
          {agents.map((a) => (
            <Badge key={a.role} data-testid={`live-state-${a.role}`} variant="outline">
              {a.role} · {a.state}
            </Badge>
          ))}
        </div>
        {configured && (
          <form className="flex flex-wrap items-center gap-2" onSubmit={send}>
            <select
              aria-label={t('agents.live.roleLabel')}
              className="rounded-md border bg-transparent px-2 py-1 text-sm"
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="">{t('agents.live.roleLabel')}</option>
              {agents.map((a) => (
                <option key={a.role} value={a.role}>
                  {a.role}
                </option>
              ))}
            </select>
            <Input
              aria-label={t('agents.live.messageLabel')}
              className="min-w-[16rem] flex-1"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={t('agents.live.messageLabel')}
            />
            <Button type="submit" disabled={busy || !role || !text.trim()}>
              {t('agents.live.send')}
            </Button>
            {outcome && <span className="text-sm text-[var(--color-muted-foreground)]">{outcome}</span>}
          </form>
        )}
      </CardContent>
    </Card>
  )
}

export function AgentsView() {
  const t = useT()
  const agentsState = useAsyncData(() => fetchAgents(), [])
  const verdictsState = useAsyncData(() => fetchVerdicts(undefined, SEVERITY_VERDICTS_LIMIT), [])
  const [selectedRole, setSelectedRole] = useState<string | null>(null)

  const isAgentsForbidden = agentsState.status === 'error' && agentsState.error instanceof ApiForbiddenError
  const isVerdictsForbidden = verdictsState.status === 'error' && verdictsState.error instanceof ApiForbiddenError
  const byRole = verdictsState.status === 'success' ? verdictsState.data.by_role : undefined

  const agents = agentsState.status === 'success' ? agentsState.data.agents : undefined

  const rows: RosterRow[] = useMemo(() => {
    if (!agents) return []
    return agents
      .map((agent) => {
        const nonNominalVerdict = hasNonNominalVerdict(byRole?.[agent.name])
        return { agent, nonNominalVerdict, tone: roleTone(agent, nonNominalVerdict) }
      })
      .sort(
        (a, b) =>
          TONE_RANK[a.tone] - TONE_RANK[b.tone] ||
          b.agent.cost_usd - a.agent.cost_usd ||
          a.agent.name.localeCompare(b.agent.name),
      )
  }, [agents, byRole])

  const maxCost = useMemo(
    () => rows.reduce((max, row) => (row.agent.attributed ? Math.max(max, row.agent.cost_usd) : max), 0),
    [rows],
  )

  const selectedAgent = agents?.find((agent) => agent.name === selectedRole) ?? null

  return (
    <div className="flex flex-col gap-4">
      <LiveAgentsPanel />
      <Card>
        <CardHeader>
          <CardTitle>{t('agents.title')}</CardTitle>
          <CardDescription>{t('agents.description')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {isAgentsForbidden ? (
            <div
              role="alert"
              data-testid="agents-forbidden"
              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              {t('agents.forbidden')}
            </div>
          ) : (
            <AsyncSection state={agentsState} isEmpty={() => false}>
              {(data) =>
                data.agents.length === 0 ? (
                  <EmptyState
                    data-testid="agents-empty"
                    title={t('agents.noRoster')}
                    body={t('agents.noRosterBody')}
                    className="max-w-xl"
                  />
                ) : (
                  <div className="flex flex-col gap-4">
                    <AttentionBand rows={rows} onSelect={setSelectedRole} />
                    <RosterTable
                      rows={rows}
                      maxCost={maxCost}
                      selected={selectedRole}
                      onSelect={setSelectedRole}
                    />
                    <UnknownBucketCard unknown={data.unknown} />
                  </div>
                )
              }
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
