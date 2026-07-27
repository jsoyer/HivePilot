import { AlertTriangle, ArrowDownToLine, ArrowUpFromLine, DollarSign } from 'lucide-react'
import { useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { useT } from '@/lib/i18n'
import { fetchAnalyticsCost } from '@/lib/pollen-api'
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
                      {t('cost.unpricedBanner', {
                        count: data.unpriced_models.length,
                        models: data.unpriced_models.join(', '),
                      })}
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
                      <Table>
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
                    <Table>
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
    </div>
  )
}
