import { Badge } from '@/components/ui/badge'
import { useT } from '@/lib/i18n'
import { fetchPluginsHealth } from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'

/**
 * Header status pills — ONLY the plugins that are ACTIVE (health `ok`), by
 * operator decision: the header strip answers "what is live", nothing else.
 *
 * It once rendered one pill per health entry, and five default-enabled but
 * never-used plugins (bitwarden, vaultwarden, infisical, kms, hugo) filled
 * the header with permanent red — read, reasonably, as "plugins in error".
 * The full tri-state picture (ok/degraded/error, with details) belongs to
 * the Health tab; the header never replaces it, and a configured-but-broken
 * plugin is the Health tab's job to surface, not this strip's.
 *
 * Data source unchanged: the same `/v1/plugins/health` fetch `HealthView`
 * uses, fetched independently because the header must render regardless of
 * which sidebar view is active. Loading and error states both render
 * nothing — a transient/failed health check must never crash or clutter the
 * header.
 */
export function StatusPills() {
  const t = useT()
  const health = useAsyncData(() => fetchPluginsHealth(), [])

  if (health.status !== 'success') {
    return null
  }
  const active = health.data.plugins.filter((plugin) => plugin.status === 'ok')
  if (active.length === 0) {
    return null
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="status-pills">
      {active.map((plugin) => (
        <Badge
          key={plugin.name}
          data-testid="status-pill"
          variant="secondary"
          title={plugin.detail || `${plugin.name}: ${t('health.status.ok')}`}
          className="gap-1.5"
        >
          {/* visual identity: the pulsing phosphor "live" dot (motion-safe
           * only) — every pill here is a healthy plugin, matching the
           * reference mockup's signature pulsing-green LIVE indicator. */}
          <span
            aria-hidden="true"
            data-testid="status-pill-dot"
            className="inline-block size-1.5 shrink-0 rounded-full bg-(--color-good) shadow-[0_0_6px_var(--color-good)] motion-safe:animate-pulse"
          />
          {plugin.name} {t('health.status.ok')}
        </Badge>
      ))}
    </div>
  )
}
