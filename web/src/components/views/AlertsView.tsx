import { describeApiError } from '@/lib/format-error'
import { EM_DASH, formatAge, formatTimestamp } from '@/lib/format-time'
import { useT, type TranslationKey } from '@/lib/i18n'
import { buildAlertFeed, type AlertItem, type AlertKind } from '@/lib/alert-feed'
import { ApiForbiddenError } from '@/lib/api'
import {
  fetchHealthProbes,
  fetchPluginsHealth,
  fetchRuns,
  type HealthProbes,
  type PluginsHealthResponse,
  type RunSummary,
} from '@/lib/pollen-api'
import { useAsyncData, type AsyncState } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'

const KIND_LABEL: Record<AlertKind, TranslationKey> = {
  failed: 'alerts.kind.failed',
  degraded: 'alerts.kind.degraded',
  classifier: 'alerts.kind.classifier',
}

const KIND_TONE: Record<AlertKind, string> = {
  failed: 'text-[var(--color-crit)]',
  degraded: 'text-[var(--color-warn)]',
  classifier: 'text-primary',
}

function settled<T>(state: AsyncState<T>): T | null {
  return state.status === 'success' ? state.data : null
}

function rowTitle(item: AlertItem, t: (key: TranslationKey) => string): string {
  if (item.kind === 'classifier') {
    return item.classifierState === 'down' ? t('alerts.classifier.down') : t('alerts.classifier.healthy')
  }
  return item.title
}

function rowMeta(item: AlertItem, t: (key: TranslationKey) => string): string {
  if (item.kind === 'classifier') {
    const pulse =
      item.classifierState === 'down' ? t('alerts.classifier.unreachable') : t('alerts.classifier.reachable')
    return item.backend ? `${item.backend} · ${pulse}` : pulse
  }
  if (item.metaKey === 'installedDisabled') return t('alerts.meta.installedDisabled')
  if (item.metaKey === 'degradedFallback') return t('alerts.meta.degradedFallback')
  return item.meta
}

function AlertRow({ item }: { item: AlertItem }) {
  const t = useT()
  const title = rowTitle(item, t)
  const meta = rowMeta(item, t)
  const age = formatAge(item.at)

  return (
    <li
      data-testid="alert-row"
      data-kind={item.kind}
      data-alert-id={item.id}
      className="flex items-start justify-between gap-4 rounded-[10px] border border-border bg-card px-4 py-3"
    >
      <div className="flex min-w-0 items-start gap-3">
        <span
          className={cn(
            'mt-0.5 shrink-0 text-[11px] font-semibold tracking-[0.08em] uppercase',
            KIND_TONE[item.kind],
          )}
        >
          {t(KIND_LABEL[item.kind])}
        </span>
        <div className="min-w-0">
          <div className="truncate font-medium text-foreground">{title}</div>
          {meta ? <div className="truncate text-sm text-muted-foreground">{meta}</div> : null}
        </div>
      </div>
      <span
        className="metric-mono shrink-0 text-sm text-muted-foreground"
        title={item.at ? formatTimestamp(item.at) : undefined}
      >
        {age}
      </span>
    </li>
  )
}

/**
 * Alerts door — worst-first list (redesign PR3).
 *
 * Failed runs, then degraded plugins, then classifier health. Same feed the
 * header chip counts. The Health dump stays under Plus → System.
 */
export function AlertsView() {
  const t = useT()
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

  const loading =
    health.status === 'loading' || runs.status === 'loading' || probes.status === 'loading'
  const hardError = !loading && health.status === 'error' && runs.status === 'error'
  const loadError = health.status === 'error' ? health.error : runs.status === 'error' ? runs.error : null

  const healthData: PluginsHealthResponse | null = settled(health)
  const runData = settled(runs) ?? []
  const surface = settled(probes)?.agent_surface ?? null

  const items = buildAlertFeed({
    plugins: healthData?.plugins ?? [],
    disabled: healthData?.disabled ?? [],
    denied: healthData?.denied ?? [],
    notInstalled: healthData?.not_installed ?? [],
    runs: runData,
    agentSurface: surface,
  })

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-4" data-testid="alerts-page">
      <header className="flex flex-col gap-1">
        <h2 className="text-3xl font-semibold tracking-tight">{t('alerts.title')}</h2>
        <p className="text-sm text-muted-foreground">{t('alerts.subtitle')}</p>
      </header>
      {loading && items.length === 0 ? (
        <p className="text-sm text-muted-foreground" role="status">
          {t('common.loading')}
        </p>
      ) : hardError ? (
        <p role="alert" className="text-sm text-destructive">
          {describeApiError(loadError)}
        </p>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="alerts-empty">
          {t('alerts.empty')}
        </p>
      ) : (
        <ul className="flex flex-col gap-2" data-testid="alerts-list">
          {items.map((item) => (
            <AlertRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </div>
  )
}
