import { AlertTriangle, ArrowDownToLine, ArrowUpFromLine, DollarSign } from 'lucide-react'
import { useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { useT } from '@/lib/i18n'
import {
  fetchAnalyticsCost,
  fetchAnalyticsWhales,
  fetchSessionCosts,
  type CostBasisFigure,
  type CostBasisReport,
} from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'
import { AsyncSection } from './AsyncSection'

/** The 3 window options this view's selector cycles through — the sprint
 * calls these out explicitly (1/7/30 days), unlike every other Pollen view
 * which uses a single fixed window. */
const WINDOW_OPTIONS = [1, 7, 30] as const
type WindowDays = (typeof WINDOW_OPTIONS)[number]

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

function formatPercent(part: number, total: number): string {
  if (total <= 0) return '—'
  return `${Math.round((part / total) * 100)}%`
}

interface WindowSelectorProps {
  value: WindowDays
  onChange: (days: WindowDays) => void
}

/**
 * The days: 1/7/30 window toggle — a plain `Button` group (no dedicated
 * `Select`/toggle-group primitive exists in `@/components/ui` yet), styled
 * with `aria-pressed` so it reads as a toggle group to assistive tech.
 */
function WindowSelector({ value, onChange }: WindowSelectorProps) {
  const t = useT()
  return (
    <div
      role="group"
      aria-label={t('cost.windowSelectorLabel')}
      className="flex gap-1"
    >
      {WINDOW_OPTIONS.map((days) => (
        <Button
          key={days}
          type="button"
          size="sm"
          variant={value === days ? 'default' : 'outline'}
          aria-pressed={value === days}
          data-testid={`cost-window-${days}`}
          onClick={() => onChange(days)}
        >
          {t('cost.windowDays', { days })}
        </Button>
      ))}
    </div>
  )
}

/**
 * Which BASIS each cost figure came from.
 *
 * Everything else on this view is the ENVELOPE — `steps.cost_usd`,
 * self-reported by the agent in `--print` mode. OTel exports the same spend
 * independently, per API request. They are two readings of one number, so
 * there is deliberately no combined total: adding them double-counts.
 *
 * And they are shown with their WINDOWS, because on the box today they do not
 * cover the same period — envelope from 2026-07-26, OTel only from 2026-08-10.
 * That is a 2.4x gap between two totals that are both correct, and side by
 * side without the dates it reads as catastrophic telemetry loss.
 */
function CostBasisPanel({ basis }: { basis: CostBasisReport | null | undefined }) {
  const t = useT()

  // `undefined` as well as `null`: a server older than this field omits it,
  // and crashing here would take the view down over a stale payload.
  if (!basis) return null

  function figure(label: string, value: CostBasisFigure | null) {
    if (!value) {
      // NOT "$0.00". Absent means nothing ever reported; zero means a period
      // in which nothing was spent, and a dead exporter must not read as a
      // free week.
      return (
        <span className="text-muted-foreground">
          {label}: {t('cost.basisNotMeasured')}
        </span>
      )
    }
    return (
      <span>
        {label}: ${value.total_usd.toFixed(2)}{' '}
        <span className="text-muted-foreground">
          ({value.count}, {value.first ?? '?'} → {value.last ?? '?'})
        </span>
      </span>
    )
  }

  return (
    <div data-testid="cost-basis" className="flex flex-col gap-1 text-xs">
      <div className="flex flex-wrap gap-4">
        {figure(t('cost.basisEnvelope'), basis.envelope)}
        {figure(t('cost.basisOtel'), basis.otel)}
      </div>
      <p className="text-muted-foreground" data-testid="cost-basis-note">
        {basis.comparable
          ? t('cost.basisDivergence', { pct: basis.divergence_pct ?? 0 })
          : t('cost.basisNotComparable')}
      </p>
    </div>
  )
}


/**
 * Cost tab (Mirador Spend section rebuild) — `GET /v1/analytics/cost`:
 * total spend, per-model and per-project breakdowns, and an unpriced-models
 * coverage banner. Rebuilt from the old "broken/monotone" version: no fake
 * daily-budget gauge (no such field exists anywhere in the API — see
 * `AnalyticsCost`'s docstring in `@/lib/pollen-api`), and no synthetic
 * spend-over-time trend (`cost_summary` returns one total per window, not a
 * per-day series — `fetchAnalyticsTrends` tracks run counts, not cost; a
 * single-point Sparkline would fabricate a "trend" out of one number, so
 * it's intentionally omitted here rather than faked).
 */
export function CostView() {
  const t = useT()
  const [days, setDays] = useState<WindowDays>(30)
  const cost = useAsyncData(() => fetchAnalyticsCost(days), [days])
  const whales = useAsyncData(() => fetchAnalyticsWhales(days), [days])
  const sessions = useAsyncData(() => fetchSessionCosts(days), [days])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle>{t('cost.title')}</CardTitle>
            <CardDescription>{t('common.lastDays', { days })}</CardDescription>
          </div>
          <WindowSelector value={days} onChange={setDays} />
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection state={cost} isEmpty={(data) => data.overall.total_steps === 0} emptyMessage={t('cost.noCost')}>
            {(data) => (
              <>
                <CostBasisPanel basis={data.basis} />
                {data.unpriced_models.length > 0 && (
                  <div
                    data-testid="cost-unpriced-banner"
                    role="status"
                    className={cn(
                      'flex items-start gap-2 rounded-lg border p-3 text-sm',
                      'border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 text-[var(--color-warn)]',
                    )}
                  >
                    <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                    <span>
                      {/* Name the subsystem that is actually at fault. Blaming the
                          price map when the model IS in it, and only the tokens are
                          missing, sends the operator to the wrong config. */}
                      {t(
                        (data.overall.unpriced_reasons?.no_usage_captured ?? 0) >=
                          (data.overall.unpriced_reasons?.no_price_for_model ?? 0)
                          ? 'cost.unpricedBannerUsage'
                          : 'cost.unpricedBanner',
                        {
                          count: data.unpriced_models.length,
                          models: data.unpriced_models.join(', '),
                        },
                      )}
                    </span>
                  </div>
                )}

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <MetricReadout
                    icon={<DollarSign className="size-4" />}
                    label={t('cost.totalCost')}
                    value={formatCost(data.overall.cost_usd)}
                    tone="good"
                  />
                  <MetricReadout
                    icon={<ArrowDownToLine className="size-4" />}
                    label={t('cost.inputTokens')}
                    value={formatTokens(data.overall.input_tokens)}
                  />
                  <MetricReadout
                    icon={<ArrowUpFromLine className="size-4" />}
                    label={t('cost.outputTokens')}
                    value={formatTokens(data.overall.output_tokens)}
                  />
                  {data.overall.unpriced_steps > 0 && (
                    <MetricReadout
                      icon={<AlertTriangle className="size-4" />}
                      label={t('cost.unpricedSteps')}
                      value={data.overall.unpriced_steps}
                      tone="warn"
                    />
                  )}
                </div>

                <div className="flex flex-col gap-3">
                  <div>
                    <h3 className="text-sm font-medium">{t('cost.byModelTitle')}</h3>
                    <p className="text-xs text-muted-foreground">{t('cost.byModelDescription')}</p>
                  </div>
                  {data.by_model.length === 0 ? (
                    <p className="text-sm text-muted-foreground">{t('cost.noByModel')}</p>
                  ) : (
                    <>
                      <DistributionBar
                        segments={data.by_model.map((row) => ({ key: row.model, label: row.model, value: row.cost_usd }))}
                        total={data.overall.cost_usd}
                      />
                      <Table scrollLabel={t('cost.byModelScrollLabel')}>
                        <TableHeader>
                          <TableRow>
                            <TableHead>{t('cost.model')}</TableHead>
                            <TableHead>{t('cost.costLabel')}</TableHead>
                            <TableHead>{t('cost.percentOfTotal')}</TableHead>
                            <TableHead>{t('cost.tokensInOut')}</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {data.by_model.map((row) => (
                            <TableRow key={row.model}>
                              <TableCell>{row.model}</TableCell>
                              <TableCell>{formatCost(row.cost_usd)}</TableCell>
                              <TableCell>{formatPercent(row.cost_usd, data.overall.cost_usd)}</TableCell>
                              <TableCell>
                                {formatTokens(row.input_tokens)} / {formatTokens(row.output_tokens)}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </>
                  )}
                </div>

                <div className="flex flex-col gap-3">
                  <div>
                    <h3 className="text-sm font-medium">{t('cost.byProjectTitle')}</h3>
                    <p className="text-xs text-muted-foreground">{t('cost.byProjectDescription')}</p>
                  </div>
                  {data.by_project.length === 0 ? (
                    <p className="text-sm text-muted-foreground">{t('cost.noByProject')}</p>
                  ) : (
                    <Table scrollLabel={t('cost.byProjectScrollLabel')}>
                      <TableHeader>
                        <TableRow>
                          <TableHead>{t('common.project')}</TableHead>
                          <TableHead>{t('cost.costLabel')}</TableHead>
                          <TableHead>{t('cost.percentOfTotal')}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.by_project.map((row) => (
                          <TableRow key={row.project}>
                            <TableCell>{row.project}</TableCell>
                            <TableCell>{formatCost(row.cost_usd)}</TableCell>
                            <TableCell>{formatPercent(row.cost_usd, data.overall.cost_usd)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </div>
              </>
            )}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('cost.whalesTitle')}</CardTitle>
          <CardDescription>{t('cost.whalesDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={whales}
            isEmpty={(data) => data.whales.length === 0}
            emptyMessage={t('cost.noWhales')}
          >
            {(data) => (
              <Table scrollLabel={t('cost.whalesScrollLabel')}>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('cost.whaleStep')}</TableHead>
                    <TableHead>{t('cost.model')}</TableHead>
                    <TableHead className="text-right">{t('cost.tokensInOut')}</TableHead>
                    <TableHead className="text-right">{t('cost.costLabel')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.whales.map((w) => (
                    <TableRow key={w.step_id} data-testid={`whale-step-${w.step_id}`}>
                      <TableCell>
                        <span className="font-medium">{w.task}</span>{' '}
                        <span className="text-xs text-muted-foreground">
                          {w.step} #{w.run_id}
                        </span>
                        {!w.priced && (
                          <span className="ml-2 text-xs text-amber-600 dark:text-amber-500">
                            {t('cost.whaleUnpriced')}
                          </span>
                        )}
                      </TableCell>
                      <TableCell>{w.model}</TableCell>
                      <TableCell className="metric-mono text-right tabular-nums">
                        {formatTokens(w.input_tokens)} / {formatTokens(w.output_tokens)}
                      </TableCell>
                      <TableCell className="metric-mono text-right tabular-nums">
                        {formatCost(w.cost_usd)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('cost.sessionsTitle')}</CardTitle>
          <CardDescription>{t('cost.sessionsDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={sessions}
            isEmpty={(data) => data.sessions.length === 0}
            emptyMessage={t('cost.noSessions')}
          >
            {(data) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('cost.sessionRun')}</TableHead>
                    <TableHead className="text-right">{t('cost.sessionTotal')}</TableHead>
                    <TableHead className="text-right">{t('cost.sessionOutput')}</TableHead>
                    <TableHead className="text-right">{t('cost.sessionInput')}</TableHead>
                    <TableHead className="text-right">{t('cost.sessionCacheRead')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.sessions.map((s) => (
                    <TableRow key={s.run_id} data-testid={`session-cost-${s.run_id}`}>
                      <TableCell>
                        <span className="font-medium">{s.task}</span>{' '}
                        <span className="text-xs text-muted-foreground">#{s.run_id}</span>
                        {/* A partly-unpriceable session must never read as a
                            cheap one, so the gap is stated next to the total
                            rather than left to be inferred from its absence. */}
                        {s.unpriced_steps > 0 && (
                          <span className="ml-2 text-xs text-amber-600 dark:text-amber-500">
                            {t('cost.sessionUnpriced', { count: s.unpriced_steps })}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="metric-mono text-right tabular-nums">
                        {formatCost(s.cost_usd)}
                      </TableCell>
                      {/* Cost per component, not token counts. 517k cache
                          reads next to 20k output tokens reads as "they read
                          too much"; the same two priced read as "they write
                          a lot and the reading is cheap". */}
                      <TableCell className="metric-mono text-right tabular-nums">
                        {formatCost(s.by_component.output)}
                      </TableCell>
                      <TableCell className="metric-mono text-right tabular-nums">
                        {formatCost(s.by_component.input)}
                      </TableCell>
                      <TableCell className="metric-mono text-right tabular-nums text-muted-foreground">
                        {formatCost(s.by_component.cache_read)}
                        <span className="ml-1 text-xs">
                          ({(s.cache_read_tokens / 1000).toFixed(0)}k tok)
                        </span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
