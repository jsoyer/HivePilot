import { Cpu, Layers } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { useT } from '@/lib/i18n'
import { fetchModels, type SuccessRate } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'
import { AsyncSection } from './AsyncSection'

/** Fixed 30-day window — matches every other Mirador view's default
 * (`AnalyticsView`/`HealthView`/`HomeView`'s efficiency fetch); the sprint
 * only calls out a window selector for the Cost view, not this one. */
const DAYS = 30

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
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

/**
 * Per-model success rate cell — `SuccessRate` is `number | null`
 * (`_attempt_success_rate` excludes skipped/other from its denominator and
 * returns `null` on zero attempts, never a misleading `0`). `null` renders
 * an honest "No attempts" instead of a fabricated percentage.
 */
function SuccessRateCell({ rate }: { rate: SuccessRate }) {
  const t = useT()
  if (rate === null) {
    return <span className="text-muted-foreground">{t('models.noAttempts')}</span>
  }
  return <span className={cn('metric-mono', RATE_TONE_CLASS[rateTone(rate)])}>{Math.round(rate * 100)}%</span>
}

/**
 * Models tab (Mirador Spend section) — `GET /v1/models`: per-model cost,
 * token volume, share of spend, and success rate, plus an overall
 * cost-per-successful-run hero figure. `latency_available` is always
 * `false` today (see `analytics_service.models_summary`'s
 * `_LATENCY_UNAVAILABLE_NOTE`) — rendered as an honest "not available"
 * message rather than a fabricated p50/p95.
 */
export function ModelsView() {
  const t = useT()
  const models = useAsyncData(() => fetchModels(DAYS), [DAYS])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('models.title')}</CardTitle>
          <CardDescription>{t('common.lastDays', { days: DAYS })}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection state={models} isEmpty={(data) => data.models.length === 0} emptyMessage={t('models.noModels')}>
            {(data) => (
              <>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <MetricReadout
                    icon={<Cpu className="size-4" />}
                    label={t('models.costPerSuccessfulRun')}
                    value={
                      data.overall.cost_per_successful_run === null
                        ? t('models.noSucceededRuns')
                        : formatCost(data.overall.cost_per_successful_run)
                    }
                    sub={t('models.costPerSuccessfulRunSub')}
                    tone={data.overall.cost_per_successful_run === null ? 'default' : 'good'}
                  />
                </div>

                <div className="flex flex-col gap-2">
                  <div>
                    <h3 className="text-sm font-medium">{t('models.shareOfSpendTitle')}</h3>
                    <p className="text-xs text-muted-foreground">{t('models.shareOfSpendDescription')}</p>
                  </div>
                  <DistributionBar
                    segments={data.models.map((row) => ({ key: row.model, label: row.model, value: row.cost_usd }))}
                  />
                </div>

                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('models.title')}</TableHead>
                      <TableHead>{t('models.successRate')}</TableHead>
                      <TableHead>{t('cost.costLabel')}</TableHead>
                      <TableHead>{t('cost.tokensInOut')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.models.map((row) => (
                      <TableRow key={row.model}>
                        <TableCell>{row.model}</TableCell>
                        <TableCell>
                          <SuccessRateCell rate={row.success_rate} />
                        </TableCell>
                        <TableCell>{formatCost(row.cost_usd)}</TableCell>
                        <TableCell>
                          {formatTokens(row.input_tokens)} / {formatTokens(row.output_tokens)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>

                <div
                  data-testid="models-latency-note"
                  className="flex items-start gap-2 rounded-lg border border-border bg-muted/40 p-3 text-sm text-muted-foreground"
                >
                  <Layers className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <div>
                    <p className="font-medium text-foreground">{t('models.latencyTitle')}</p>
                    <p>{t('models.latencyNotAvailable')}</p>
                  </div>
                </div>
              </>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
