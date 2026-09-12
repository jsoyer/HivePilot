import { ApiForbiddenError } from '@/lib/api'
import { buildAlertFeed, countAlertIssues } from '@/lib/alert-feed'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  fetchAnalyticsCost,
  fetchAnalyticsSummary,
  fetchApprovals,
  fetchHealthProbes,
  fetchPluginsHealth,
  fetchRuns,
  type HealthProbes,
  type PluginsHealthResponse,
  type RunSummary,
} from '@/lib/pollen-api'
import { useAsyncData, type AsyncState } from '@/lib/use-async-data'
import { usePersistedState } from '@/lib/use-persisted-state'
import { cn } from '@/lib/utils'

const TODAY_DAYS = 1
const SNAPSHOT_KEY = 'hivepilot.webui.inbox-snapshot-open'

export interface InboxSnapshotProps {
  onNavigate: (view: string) => void
}

function settled<T>(state: AsyncState<T>): T | null {
  return state.status === 'success' ? state.data : null
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

function formatRate(rate: number | null): string {
  if (rate === null) return '—'
  return `${Math.round(rate * 100)}%`
}

function KpiCell({
  testId,
  label,
  value,
  sub,
  tone = 'default',
}: {
  testId: string
  label: string
  value: string
  sub: string
  tone?: 'default' | 'good' | 'crit'
}) {
  const valueClass =
    tone === 'good'
      ? 'text-[var(--color-good)]'
      : tone === 'crit'
        ? 'text-[var(--color-crit)]'
        : 'text-foreground'
  return (
    <div
      data-testid={testId}
      className="rounded-[12px] border border-border bg-card px-4 py-3"
    >
      <p className={cn('metric-mono text-xl font-semibold tracking-tight', valueClass)}>{value}</p>
      <p className="mt-1 text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
        {label}
      </p>
      <p className="text-xs text-muted-foreground">{sub}</p>
    </div>
  )
}

function kpiValue<T>(state: AsyncState<T>, render: (data: T) => { value: string; sub: string; tone?: 'default' | 'good' | 'crit' }, loading: string, roleDenied: string): {
  value: string
  sub: string
  tone?: 'default' | 'good' | 'crit'
} {
  if (state.status === 'loading') return { value: '—', sub: loading }
  if (state.status === 'error') {
    return {
      value: '—',
      sub: state.error instanceof ApiForbiddenError ? roleDenied : describeApiError(state.error),
    }
  }
  return render(state.data)
}

/**
 * Collapsible Inbox snapshot (redesign PR4) — three KPIs plus a compact
 * Alerts strip. Not a Home landing: no SweepRadar, no host strip.
 */
export function InboxSnapshot({ onNavigate }: InboxSnapshotProps) {
  const t = useT()
  const [open, setOpen] = usePersistedState<boolean>(SNAPSHOT_KEY, true)

  const cost = useAsyncData(() => fetchAnalyticsCost(TODAY_DAYS), [])
  const summary = useAsyncData(() => fetchAnalyticsSummary(TODAY_DAYS), [])
  const approvals = useAsyncData(() => fetchApprovals(), [])
  const health = useAsyncData(() => fetchPluginsHealth(), [])
  const runs = useAsyncData(async (): Promise<RunSummary[]> => {
    try {
      return await fetchRuns(50)
    } catch (error) {
      if (error instanceof ApiForbiddenError) return []
      throw error
    }
  }, [])
  const probes = useAsyncData((): Promise<HealthProbes | null> => fetchHealthProbes().catch(() => null), [])

  const healthData: PluginsHealthResponse | null = settled(health)
  const items = buildAlertFeed({
    plugins: healthData?.plugins ?? [],
    disabled: healthData?.disabled ?? [],
    denied: healthData?.denied ?? [],
    notInstalled: healthData?.not_installed ?? [],
    runs: settled(runs) ?? [],
    agentSurface: settled(probes)?.agent_surface ?? null,
  })
  const issueCount = countAlertIssues(items)
  const failed = items.filter((item) => item.kind === 'failed').length
  const degraded = items.filter((item) => item.kind === 'degraded').length

  const spend = kpiValue(
    cost,
    (data) => ({ value: formatCost(data.overall.cost_usd), sub: t('inbox.kpiSpendSub') }),
    t('common.loading'),
    t('home.kpiRequiresRole'),
  )
  const success = kpiValue(
    summary,
    (data) => {
      const tone = data.success_rate === null ? 'default' : data.success_rate >= 0.8 ? 'good' : data.success_rate >= 0.5 ? 'default' : 'crit'
      return {
        value: formatRate(data.success_rate),
        sub: t('inbox.kpiSuccessSub', { count: data.total }),
        tone,
      }
    },
    t('common.loading'),
    t('home.kpiRequiresRole'),
  )
  const pending = kpiValue(
    approvals,
    (data) => ({
      value: String(data.length),
      sub: t('inbox.kpiPendingSub'),
      tone: data.length > 0 ? 'crit' : 'default',
    }),
    t('common.loading'),
    t('home.kpiRequiresRole'),
  )

  return (
    <div data-testid="inbox-snapshot" className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="text-3xl font-semibold tracking-tight">{t('inbox.title')}</h2>
          <p className="text-sm text-muted-foreground">{t('inbox.subtitle')}</p>
        </div>
        <button
          type="button"
          data-testid="inbox-snapshot-toggle"
          aria-expanded={open}
          aria-label={open ? t('inbox.snapshotCollapse') : t('inbox.snapshotExpand')}
          onClick={() => setOpen((current) => !current)}
          className="shrink-0 rounded-full border border-border bg-card px-3 py-1 text-xs text-muted-foreground"
        >
          {t('inbox.snapshot')} {open ? '▾' : '▸'}
        </button>
      </div>
      {open ? (
        <>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <KpiCell
              testId="inbox-kpi-spend"
              label={t('inbox.kpiSpend')}
              value={spend.value}
              sub={spend.sub}
              tone={spend.tone}
            />
            <KpiCell
              testId="inbox-kpi-success"
              label={t('inbox.kpiSuccess')}
              value={success.value}
              sub={success.sub}
              tone={success.tone}
            />
            <KpiCell
              testId="inbox-kpi-pending"
              label={t('inbox.kpiPending')}
              value={pending.value}
              sub={pending.sub}
              tone={pending.tone}
            />
          </div>
          <button
            type="button"
            data-testid="inbox-alerts-strip"
            onClick={() => onNavigate('alerts')}
            className="flex w-full items-center justify-between gap-3 rounded-[12px] border border-border bg-card px-4 py-2.5 text-left text-sm"
          >
            <span>
              <span className="font-medium">{t('header.issues', { count: issueCount })}</span>
              <span className="ml-2 text-muted-foreground">
                {t('inbox.issuesBreakdown', { failed, degraded })}
              </span>
            </span>
            <span className="shrink-0 text-muted-foreground">
              {t('inbox.openAlerts')}
              {' →'}
            </span>
          </button>
        </>
      ) : null}
    </div>
  )
}
