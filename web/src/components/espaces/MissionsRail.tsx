import { StatusGlyph } from '@/components/dashboard/StatusGlyph'
import { ApiForbiddenError } from '@/lib/api'
import { formatAge, formatTimestamp } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import { fetchRuns, type RunSummary } from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'

const RAIL_LIMIT = 12

/**
 * Compact live-missions rail (HP-80) — recent runs beside an Espaces thread.
 * Reuses HP-42 status zones + HP-44 glyphs. Classify-only display: no dispatch.
 */
export function MissionsRail({ refreshKey = 0 }: { refreshKey?: number }) {
  const t = useT()
  const state = useAsyncData(() => fetchRuns(RAIL_LIMIT), [refreshKey])

  return (
    <aside
      data-testid="espaces-rail"
      className="flex max-h-[32rem] flex-col gap-2 rounded-lg border border-border p-3 sm:col-span-2 lg:col-span-1"
    >
      <h3 className="text-sm font-semibold">{t('spaces.railTitle')}</h3>
      {state.status === 'loading' && (
        <p role="status" className="animate-pulse text-xs text-muted-foreground">
          {t('common.loading')}
        </p>
      )}
      {state.status === 'error' && (
        <p role="alert" className="text-xs text-muted-foreground">
          {state.error instanceof ApiForbiddenError ? t('spaces.railForbidden') : t('spaces.railEmpty')}
        </p>
      )}
      {state.status === 'success' && state.data.length === 0 && (
        <p className="text-xs text-muted-foreground">{t('spaces.railEmpty')}</p>
      )}
      {state.status === 'success' && state.data.length > 0 && (
        <ul className="flex flex-col gap-1 overflow-y-auto">
          {state.data.map((run) => (
            <RailRow key={run.id} run={run} />
          ))}
        </ul>
      )}
    </aside>
  )
}

function RailRow({ run }: { run: RunSummary }) {
  const t = useT()
  const heartbeat = run.last_activity_at ?? run.started_at
  return (
    <li
      data-testid={`espaces-rail-row-${run.id}`}
      className="flex items-start gap-2 rounded-md px-1.5 py-1.5"
    >
      <StatusGlyph status={run.status} label={run.status} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium">{run.project}</p>
        <p className="truncate text-[11px] text-muted-foreground">
          {run.task}
          <span className="metric-mono"> · #{run.id}</span>
        </p>
        <p className="text-[10px] text-muted-foreground" title={formatTimestamp(heartbeat)}>
          {run.last_activity_at
            ? t('board.lastHeartbeat', { age: formatAge(run.last_activity_at) })
            : t('board.startedAgo', { age: formatAge(run.started_at) })}
        </p>
      </div>
    </li>
  )
}
