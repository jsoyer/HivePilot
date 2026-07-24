import { Activity, CheckCircle2, Clock, XCircle } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { StatCard } from '@/components/dashboard/StatCard'
import { useT } from '@/lib/i18n'
import {
  fetchAnalyticsDurations,
  fetchAnalyticsSummary,
  fetchAnalyticsTrends,
  fetchApprovalLatency,
  fetchStepFailures,
} from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'
import { PercentileBars, TrendBarChart } from './charts'

const DAYS = 30

function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`
}

/**
 * Analytics tab — volume + outcome rates, a run-volume trend, duration
 * percentiles, step-failure hotspots, and approval latency. Each section
 * fetches independently (`useAsyncData` per endpoint) so one endpoint
 * failing never blanks the rest of the panel.
 */
export function AnalyticsView() {
  const t = useT()
  const summary = useAsyncData(() => fetchAnalyticsSummary(DAYS), [DAYS])
  const trends = useAsyncData(() => fetchAnalyticsTrends(DAYS, 'day'), [DAYS])
  const durations = useAsyncData(() => fetchAnalyticsDurations(DAYS), [DAYS])
  const hotspots = useAsyncData(() => fetchStepFailures(DAYS), [DAYS])
  const approvalLatency = useAsyncData(() => fetchApprovalLatency(DAYS), [DAYS])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('analytics.volumeTitle')}</CardTitle>
          <CardDescription>{t('common.lastDays', { days: DAYS })}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection state={summary} isEmpty={(data) => data.total === 0} emptyMessage={t('analytics.noRuns')}>
            {(data) => (
              <>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                  <StatCard
                    icon={<Activity className="size-4" />}
                    label={t('analytics.totalRuns')}
                    value={data.total}
                    sub={t('common.lastDaysLower', { days: DAYS })}
                  />
                  <StatCard
                    icon={<CheckCircle2 className="size-4" />}
                    label={t('analytics.succeeded')}
                    value={pct(data.outcome_rates.succeeded)}
                    sub={t('analytics.runsCount', { count: data.outcomes.succeeded })}
                    tone="positive"
                  />
                  <StatCard
                    icon={<XCircle className="size-4" />}
                    label={t('analytics.failed')}
                    value={pct(data.outcome_rates.failed)}
                    sub={t('analytics.runsCount', { count: data.outcomes.failed })}
                    tone="danger"
                  />
                </div>
                <DistributionBar
                  segments={[
                    {
                      key: 'succeeded',
                      label: t('analytics.succeeded'),
                      value: data.outcomes.succeeded,
                      colorClass: 'bg-emerald-500',
                    },
                    {
                      key: 'failed',
                      label: t('analytics.failed'),
                      value: data.outcomes.failed,
                      colorClass: 'bg-destructive',
                    },
                    {
                      key: 'other',
                      label: t('analytics.other'),
                      value: data.outcomes.other,
                      colorClass: 'bg-muted-foreground/40',
                    },
                  ]}
                  total={data.total}
                />
              </>
            )}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('analytics.trendTitle')}</CardTitle>
          <CardDescription>{t('analytics.trendDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={trends}
            isEmpty={(data) => data.series.length === 0}
            emptyMessage={t('analytics.noTrend')}
          >
            {(data) => <TrendBarChart series={data.series} />}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('analytics.durationTitle')}</CardTitle>
          <CardDescription>{t('analytics.durationDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={durations}
            isEmpty={(data) => data.overall.count === 0}
            emptyMessage={t('analytics.noDuration')}
          >
            {(data) => <PercentileBars stats={data.overall} />}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('analytics.hotspotsTitle')}</CardTitle>
          <CardDescription>{t('analytics.hotspotsDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={hotspots}
            isEmpty={(data) => data.hotspots.length === 0}
            emptyMessage={t('analytics.noHotspots')}
          >
            {(data) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('analytics.step')}</TableHead>
                    <TableHead>{t('analytics.status')}</TableHead>
                    <TableHead>{t('analytics.count')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.hotspots.map((hotspot) => (
                    <TableRow key={`${hotspot.step}-${hotspot.status}`}>
                      <TableCell>{hotspot.step}</TableCell>
                      <TableCell>{hotspot.status}</TableCell>
                      <TableCell>{hotspot.count}</TableCell>
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
          <CardTitle>{t('analytics.approvalLatencyTitle')}</CardTitle>
          <CardDescription>{t('analytics.approvalLatencyDescription')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={approvalLatency}
            isEmpty={(data) => data.count === 0}
            emptyMessage={t('analytics.noApprovals')}
          >
            {(data) => (
              <div className="flex flex-col gap-4">
                <StatCard
                  icon={<Clock className="size-4" />}
                  label={t('analytics.actionedApprovals')}
                  value={data.count}
                />
                <PercentileBars stats={data} />
              </div>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
