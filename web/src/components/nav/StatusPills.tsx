import { Badge } from '@/components/ui/badge'
import { useT } from '@/lib/i18n'
import { fetchPluginsHealth, type PluginHealthStatus } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'

/** Same mapping `HealthView` uses for its own per-plugin badges — kept in
 * sync intentionally (both read the same `PluginHealthStatus` union), so a
 * plugin's color coding never disagrees between the header pill and the
 * Health tab's row. */
const STATUS_VARIANT: Record<PluginHealthStatus, 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  degraded: 'outline',
  error: 'destructive',
}

/** IA/Cyber identity: the small per-pill status dot's color — `ok` reads as
 * the phosphor "live" tone (matching the reference mockup's pulsing green
 * LIVE indicator), `degraded`/`error` fall back to warn/crit so a pill never
 * pulses green while unhealthy. */
const STATUS_DOT_CLASS: Record<PluginHealthStatus, string> = {
  ok: 'bg-(--color-good) shadow-[0_0_6px_var(--color-good)]',
  degraded: 'bg-(--color-warn)',
  error: 'bg-(--color-crit)',
}

/**
 * Header status pills — one per registered plugin/service, from the SAME
 * `/v1/plugins/health` data source `HealthView` already fetches (see
 * `@/lib/mirador-api`'s `fetchPluginsHealth`; no new endpoint). Deliberately
 * independent fetch: the header must show pills regardless of which sidebar
 * view is currently active, and `HealthView` only mounts (and fetches) when
 * its own tab panel is rendered.
 *
 * Loading and error states both render nothing — a transient/failed health
 * check must never crash or visually clutter the header; the Health tab
 * remains the place to see the full picture (including the error itself).
 */
export function StatusPills() {
  const t = useT()
  const health = useAsyncData(() => fetchPluginsHealth(), [])

  if (health.status !== 'success' || health.data.plugins.length === 0) {
    return null
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="status-pills">
      {health.data.plugins.map((plugin) => {
        const statusText = t(`health.status.${plugin.status}`)
        return (
          <Badge
            key={plugin.name}
            data-testid="status-pill"
            variant={STATUS_VARIANT[plugin.status]}
            title={plugin.detail || `${plugin.name}: ${statusText}`}
            className="gap-1.5"
          >
            {/* IA/Cyber identity: a small per-plugin status dot — pulses
             * (motion-safe only, see `--color-good`'s reduced-motion-safe
             * `animate-pulse`) for a healthy ("ok") plugin, matching the
             * reference mockup's signature pulsing-green LIVE indicator;
             * degraded/error dots stay static so only genuinely-live/healthy
             * status ever pulses. */}
            <span
              aria-hidden="true"
              data-testid="status-pill-dot"
              className={cn(
                'inline-block size-1.5 shrink-0 rounded-full',
                STATUS_DOT_CLASS[plugin.status],
                plugin.status === 'ok' && 'motion-safe:animate-pulse',
              )}
            />
            {plugin.name} {statusText}
          </Badge>
        )
      })}
    </div>
  )
}
