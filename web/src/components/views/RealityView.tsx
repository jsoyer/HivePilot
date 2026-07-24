import { CheckCircle2, Clock, SearchX, ShieldCheck, XCircle } from 'lucide-react'
import type { ReactNode } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { StatCard, type StatCardTone } from '@/components/dashboard/StatCard'
import { ApiForbiddenError } from '@/lib/api'
import { useT } from '@/lib/i18n'
import {
  fetchMemoryEvaluations,
  fetchMemoryGaps,
  fetchMemoryJournal,
  fetchMemoryReality,
  type MemoryJournalEntry,
} from '@/lib/mirador-api'
import type { AsyncState } from '@/lib/use-async-data'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const DAYS = 30
const EVAL_LIMIT = 50
const JOURNAL_LIMIT = 50

function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`
}

/** Rate-tinted `StatCard` tone: neutral below any real signal, positive at
 * or above 80%, warning at or above 50%, danger below that. */
function rateTone(rate: number): StatCardTone {
  if (rate >= 0.8) return 'positive'
  if (rate >= 0.5) return 'warning'
  return 'danger'
}

/** Formats a duration in seconds as a short human string ("2d 6h", "3h 12m",
 * "45m", "12s"). `null`/`undefined` (no freshness signal on this row) renders
 * as an explicit "—" rather than being conflated with a genuine `0`. */
function formatFreshness(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const total = Math.max(0, Math.round(seconds))
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m`
  return `${total}s`
}

/** A journal row's "result" column: `result_count` for `search` events,
 * `found` (✓/✗) for `read`/`store` events — `activity_journal`'s docstring
 * guarantees exactly one of the two is non-null per row. */
function formatJournalResult(entry: MemoryJournalEntry): ReactNode {
  if (entry.result_count !== null && entry.result_count !== undefined) {
    return String(entry.result_count)
  }
  if (entry.found !== null && entry.found !== undefined) {
    return entry.found ? '✓' : '✗'
  }
  return '—'
}

/** Renders `state`'s data via `AsyncSection`, except a `403` — which peels
 * off into a shared "requires a higher-privilege token" banner instead of
 * `AsyncSection`'s generic error card (mirrors `GraphView`/`PanelView`'s
 * per-section `ApiForbiddenError` handling). Keeps one section's role gate
 * from ever blanking the rest of the Réalité view. */
function ForbiddenAwareSection<T>({
  testId,
  state,
  isEmpty,
  emptyMessage,
  children,
}: {
  testId: string
  state: AsyncState<T>
  isEmpty: (data: T) => boolean
  emptyMessage: string
  children: (data: T) => ReactNode
}) {
  const t = useT()

  if (state.status === 'error' && state.error instanceof ApiForbiddenError) {
    return (
      <div
        data-testid={`reality-forbidden-${testId}`}
        className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
      >
        {t('reality.requiresTokenLead')} <span className="font-medium text-foreground">{t('reality.requiresTokenTail')}</span>{' '}
        {t('reality.requiresTokenNote')}
      </div>
    )
  }

  return (
    <AsyncSection state={state} isEmpty={isEmpty} emptyMessage={emptyMessage}>
      {children}
    </AsyncSection>
  )
}

/**
 * Réalité tab — Mirador's memory-quality dashboard, consuming
 * `/v1/memory/{reality,gaps,evaluations,journal}`. Answers "does the memory
 * substrate actually help", not just how much it holds: search
 * success/no-result rate, recall freshness, human-declared reliability,
 * gaps by namespace, recent evaluations, and a raw activity journal.
 *
 * Every section fetches independently (`useAsyncData` per endpoint, same
 * pattern as `AnalyticsView`) so one endpoint failing or 403ing never blanks
 * the rest of the panel.
 *
 * **Honesty over completeness:** when every endpoint resolves to genuinely
 * empty (no searches, no evaluations, no gaps, no journal rows — the
 * OPT-IN instrumentation has never fired), the view renders ONE plain
 * empty-state message instead of four cards full of `0`/`0%` stats that
 * would look like real (if bad) numbers. Individual rate metrics
 * (`search_success_rate`, `declared_reliability`) also fall back to "No
 * data" instead of a fabricated `0%` when their own denominator
 * (`total_searches`/`total_evaluations`) is zero, even in a otherwise
 * non-empty window — `0%` there would misleadingly claim "0% useful" rather
 * than "nobody has rated anything yet".
 *
 * **Security:** `namespace` / `query_or_key` / `note` / `actor` /
 * `top_queries` are all caller-influenced free text (untrusted — see
 * `mirador-api.ts`'s module note above the memory fetchers). Every one of
 * them is rendered via plain JSX text interpolation only — this file never
 * bypasses React's auto-escaping with a raw-HTML injection prop.
 */
export function RealityView() {
  const t = useT()
  const reality = useAsyncData(() => fetchMemoryReality(DAYS), [DAYS])
  const gaps = useAsyncData(() => fetchMemoryGaps(DAYS), [DAYS])
  const evaluations = useAsyncData(() => fetchMemoryEvaluations(EVAL_LIMIT), [EVAL_LIMIT])
  const journal = useAsyncData(() => fetchMemoryJournal(JOURNAL_LIMIT), [JOURNAL_LIMIT])

  const allEmpty =
    reality.status === 'success' &&
    reality.data.total_searches === 0 &&
    reality.data.total_evaluations === 0 &&
    gaps.status === 'success' &&
    gaps.data.gaps.length === 0 &&
    evaluations.status === 'success' &&
    evaluations.data.evaluations.length === 0 &&
    journal.status === 'success' &&
    journal.data.journal.length === 0

  if (allEmpty) {
    return (
      <Card>
        <CardContent>
          <p data-testid="reality-empty-state" className="text-sm text-muted-foreground">
            {t('reality.emptyState')}
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('reality.kpiTitle')}</CardTitle>
          <CardDescription>{t('common.lastDays', { days: DAYS })}</CardDescription>
        </CardHeader>
        <CardContent>
          <ForbiddenAwareSection
            testId="kpi"
            state={reality}
            isEmpty={(data) => data.total_searches === 0 && data.total_evaluations === 0}
            emptyMessage={t('reality.noKpiData')}
          >
            {(data) => (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <StatCard
                  icon={<SearchX className="size-4" />}
                  label={t('reality.searchSuccessRate')}
                  value={data.total_searches > 0 ? pct(data.search_success_rate) : t('reality.noSamples')}
                  sub={t('reality.onNSearches', { count: data.total_searches })}
                  tone={data.total_searches > 0 ? rateTone(data.search_success_rate) : 'default'}
                />
                <StatCard
                  icon={<SearchX className="size-4" />}
                  label={t('reality.noResultSearches')}
                  value={data.no_result_count}
                  sub={t('reality.onNSearches', { count: data.total_searches })}
                  tone={data.no_result_count > 0 ? 'warning' : 'positive'}
                />
                <StatCard
                  icon={<Clock className="size-4" />}
                  label={t('reality.avgFreshness')}
                  value={formatFreshness(data.avg_freshness_seconds)}
                />
                <StatCard
                  icon={<ShieldCheck className="size-4" />}
                  label={t('reality.declaredReliability')}
                  value={data.total_evaluations > 0 ? pct(data.declared_reliability) : t('reality.noSamples')}
                  sub={t('reality.onNEvaluations', { count: data.total_evaluations })}
                  tone={data.total_evaluations > 0 ? rateTone(data.declared_reliability) : 'default'}
                />
              </div>
            )}
          </ForbiddenAwareSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('reality.gapsTitle')}</CardTitle>
          <CardDescription>{t('reality.gapsDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <ForbiddenAwareSection
            testId="gaps"
            state={gaps}
            isEmpty={(data) => data.gaps.length === 0}
            emptyMessage={t('reality.noGaps')}
          >
            {(data) => (
              <div className="flex flex-col gap-4">
                <DistributionBar
                  segments={data.gaps.map((gap) => ({
                    key: gap.namespace,
                    label: gap.namespace,
                    value: gap.no_result_count,
                  }))}
                />
                <ul className="flex flex-col gap-2">
                  {data.gaps.map((gap) => (
                    <li key={gap.namespace} className="text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">{gap.namespace}</span>
                      {gap.top_queries.length > 0 && (
                        <>
                          {' — '}
                          {t('reality.topQueriesLabel')}{' '}
                          {gap.top_queries.map((query, index) => (
                            <span key={`${gap.namespace}-${index}`}>
                              {index > 0 && ', '}
                              &ldquo;{query}&rdquo;
                            </span>
                          ))}
                        </>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </ForbiddenAwareSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('reality.evaluationsTitle')}</CardTitle>
          <CardDescription>{t('reality.evaluationsDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <ForbiddenAwareSection
            testId="evaluations"
            state={evaluations}
            isEmpty={(data) => data.evaluations.length === 0}
            emptyMessage={t('reality.noEvaluations')}
          >
            {(data) => (
              <ul className="flex flex-col gap-2">
                {data.evaluations.map((evaluation, index) => (
                  <li
                    key={`${evaluation.ts}-${index}`}
                    className="flex flex-col gap-1 rounded-lg border border-border p-2 text-sm"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      {evaluation.useful === true && (
                        <CheckCircle2
                          className="size-4 shrink-0 text-emerald-500"
                          aria-label={t('reality.useful')}
                        />
                      )}
                      {evaluation.useful === false && (
                        <XCircle className="size-4 shrink-0 text-destructive" aria-label={t('reality.notUseful')} />
                      )}
                      {evaluation.useful === null && <span className="text-muted-foreground">—</span>}
                      <span className="font-medium">{evaluation.namespace}</span>
                      <span className="text-xs text-muted-foreground">{evaluation.ts}</span>
                      <span className="ml-auto text-xs text-muted-foreground">{evaluation.actor}</span>
                    </div>
                    {evaluation.note && <p className="text-xs text-muted-foreground">{evaluation.note}</p>}
                  </li>
                ))}
              </ul>
            )}
          </ForbiddenAwareSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('reality.journalTitle')}</CardTitle>
          <CardDescription>{t('reality.journalDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <ForbiddenAwareSection
            testId="journal"
            state={journal}
            isEmpty={(data) => data.journal.length === 0}
            emptyMessage={t('reality.noJournal')}
          >
            {(data) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('reality.colTs')}</TableHead>
                    <TableHead>{t('reality.colOp')}</TableHead>
                    <TableHead>{t('reality.colNamespace')}</TableHead>
                    <TableHead>{t('reality.colQuery')}</TableHead>
                    <TableHead>{t('reality.colResult')}</TableHead>
                    <TableHead>{t('reality.colFreshness')}</TableHead>
                    <TableHead>{t('reality.colActor')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.journal.map((entry, index) => (
                    <TableRow key={`${entry.ts}-${index}`}>
                      <TableCell>{entry.ts}</TableCell>
                      <TableCell>{entry.op}</TableCell>
                      <TableCell>{entry.namespace}</TableCell>
                      <TableCell className="whitespace-normal">{entry.query_or_key ?? '—'}</TableCell>
                      <TableCell>{formatJournalResult(entry)}</TableCell>
                      <TableCell>{formatFreshness(entry.freshness_seconds)}</TableCell>
                      <TableCell>{entry.actor}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </ForbiddenAwareSection>
        </CardContent>
      </Card>
    </div>
  )
}
