import { Badge } from '@/components/ui/badge'
import { fetchPluginsHealth, type PluginHealthStatus } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'

/** Same mapping `HealthView` uses for its own per-plugin badges — kept in
 * sync intentionally (both read the same `PluginHealthStatus` union), so a
 * plugin's color coding never disagrees between the header pill and the
 * Health tab's row. */
const STATUS_VARIANT: Record<PluginHealthStatus, 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  degraded: 'outline',
  error: 'destructive',
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
  const health = useAsyncData(() => fetchPluginsHealth(), [])

  if (health.status !== 'success' || health.data.plugins.length === 0) {
    return null
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="status-pills">
      {health.data.plugins.map((plugin) => (
        <Badge
          key={plugin.name}
          data-testid="status-pill"
          variant={STATUS_VARIANT[plugin.status]}
          title={plugin.detail || `${plugin.name}: ${plugin.status}`}
        >
          {plugin.name} {plugin.status}
        </Badge>
      ))}
    </div>
  )
}
