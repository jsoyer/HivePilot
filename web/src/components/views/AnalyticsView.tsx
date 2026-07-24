import { Activity, CheckCircle2, Clock, XCircle } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { StatCard } from '@/components/dashboard/StatCard'
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
  const summary = useAsyncData(() => fetchAnalyticsSummary(DAYS), [DAYS])
  const trends = useAsyncData(() => fetchAnalyticsTrends(DAYS, 'day'), [DAYS])
  const durations = useAsyncData(() => fetchAnalyticsDurations(DAYS), [DAYS])
  const hotspots = useAsyncData(() => fetchStepFailures(DAYS), [DAYS])
  const approvalLatency = useAsyncData(() => fetchApprovalLatency(DAYS), [DAYS])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Volume &amp; outcomes</CardTitle>
          <CardDescription>Last {DAYS} days</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection state={summary} isEmpty={(data) => data.total === 0} emptyMessage="No runs recorded in this window.">
            {(data) => (
              <>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                  <StatCard
                    icon={<Activity className="size-4" />}
                    label="Total runs"
                    value={data.total}
                    sub={`last ${DAYS} days`}
                  />
                  <StatCard
                    icon={<CheckCircle2 className="size-4" />}
                    label="Succeeded"
                    value={pct(data.outcome_rates.succeeded)}
                    sub={`${data.outcomes.succeeded} runs`}
                    tone="positive"
                  />
                  <StatCard
                    icon={<XCircle className="size-4" />}
                    label="Failed"
                    value={pct(data.outcome_rates.failed)}
                    sub={`${data.outcomes.failed} runs`}
                    tone="danger"
                  />
                </div>
                <DistributionBar
                  segments={[
                    {
                      key: 'succeeded',
                      label: 'Succeeded',
                      value: data.outcomes.succeeded,
                      colorClass: 'bg-emerald-500',
                    },
                    {
                      key: 'failed',
                      label: 'Failed',
                      value: data.outcomes.failed,
                      colorClass: 'bg-destructive',
                    },
                    {
                      key: 'other',
                      label: 'Other',
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
          <CardTitle>Trend</CardTitle>
          <CardDescription>Runs per day</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={trends}
            isEmpty={(data) => data.series.length === 0}
            emptyMessage="No trend data for this window."
          >
            {(data) => <TrendBarChart series={data.series} />}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Duration percentiles</CardTitle>
          <CardDescription>Finished runs, p50 / p95 / p99</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={durations}
            isEmpty={(data) => data.overall.count === 0}
            emptyMessage="No finished runs yet."
          >
            {(data) => <PercentileBars stats={data.overall} />}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Step failure hotspots</CardTitle>
          <CardDescription>Highest-failure-count steps first</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={hotspots}
            isEmpty={(data) => data.hotspots.length === 0}
            emptyMessage="No step failures recorded."
          >
            {(data) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Step</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Count</TableHead>
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
          <CardTitle>Approval latency</CardTitle>
          <CardDescription>Time from request to decision</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={approvalLatency}
            isEmpty={(data) => data.count === 0}
            emptyMessage="No actioned approvals yet."
          >
            {(data) => (
              <div className="flex flex-col gap-4">
                <StatCard
                  icon={<Clock className="size-4" />}
                  label="Actioned approvals"
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
