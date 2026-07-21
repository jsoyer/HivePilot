import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { fetchPluginsHealth, togglePlugin, type PluginHealthStatus } from '@/lib/mirador-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const STATUS_VARIANT: Record<PluginHealthStatus, 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  degraded: 'outline',
  error: 'destructive',
}

/** Per-row toggle result, tracked locally so a just-toggled row can show a
 * "restart required" badge immediately without waiting for (or requiring) a
 * live-reloading `GET /v1/plugins/health` -- the backend never live-applies
 * a toggle (see `togglePlugin`'s docstring in `@/lib/mirador-api`), so this
 * state is the ONLY way the UI reflects "you just changed this" this
 * session. Keyed by plugin name so one row's toggle can never affect
 * another's. */
interface ToggleState {
  disabled: boolean
  restartRequired: boolean
}

interface PluginToggleProps {
  name: string
  toggled: ToggleState | undefined
  onToggled: (name: string, result: ToggleState) => void
  /** Seed the control as already-disabled (button reads "Enable" on first
   * render) for rows sourced from `data.disabled` -- see `HealthView`'s
   * disabled-plugins section below. Ignored once `toggled` is set (a local
   * toggle result always wins over the seed). Defaults to `false` for the
   * enabled-plugin rows, which start from "Disable". */
  initialDisabled?: boolean
}

/**
 * Admin-only enable/disable control for a single plugin row. Only rendered
 * by the parent when `useRole().can('admin')` -- this component assumes the
 * caller already has an admin token; a 403 (token demoted mid-session) is
 * still handled gracefully rather than crashing the row.
 *
 * `check_all()` (what `GET /v1/plugins/health` returns) only lists currently
 * REGISTERED (i.e. enabled) plugins -- a plugin already disabled via
 * `settings.plugins_disabled` is never registered, so it never appears in
 * `data.plugins`. `GET /v1/plugins/health`'s `disabled` field closes that
 * gap: `HealthView` renders those names as their own seeded-disabled rows
 * (`initialDisabled`), so re-enabling a previously-disabled plugin (already
 * supported server-side via the union allowlist) has a row to click too.
 */
function PluginToggle({ name, toggled, onToggled, initialDisabled = false }: PluginToggleProps) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleClick() {
    setSubmitting(true)
    setError(null)
    try {
      const result = await togglePlugin(name)
      onToggled(name, { disabled: result.disabled, restartRequired: result.restart_required })
    } catch (err) {
      setError(
        err instanceof ApiForbiddenError
          ? 'Insufficient role — your token can no longer toggle plugins.'
          : describeApiError(err),
      )
    } finally {
      setSubmitting(false)
    }
  }

  const isDisabled = toggled?.disabled ?? initialDisabled

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant={isDisabled ? 'outline' : 'destructive'}
          disabled={submitting}
          onClick={() => {
            void handleClick()
          }}
          aria-label={`${isDisabled ? 'Enable' : 'Disable'} ${name}`}
          title="Takes effect on next restart only — no live reload."
        >
          {submitting ? 'Working…' : isDisabled ? 'Enable' : 'Disable'}
        </Button>
        {toggled?.restartRequired && (
          <Badge variant="outline" title="This change applies on the API server's next restart.">
            restart required
          </Badge>
        )}
      </div>
      {error && (
        <div role="alert" className="text-sm text-destructive">
          {error}
        </div>
      )}
    </div>
  )
}

/**
 * Health tab -- `GET /v1/plugins/health`, one badge per plugin, plus an
 * admin-only enable/disable toggle (`POST /v1/plugins/{name}/toggle`,
 * Mirador actionable dashboard PRD, Sprint 5).
 *
 * Non-admin tokens (`useRole().can('admin')` false) see the exact same
 * read-only rows Sprint 1 shipped -- no toggle control renders at all.
 *
 * **Dedupe (loaded-but-pending-disable):** `data.plugins` (currently
 * registered) and `data.disabled` (`settings.plugins_disabled`) are NOT
 * mutually exclusive -- a plugin stays registered until the next restart
 * (see `PluginToggle`'s docstring), so clicking "Disable" and reloading the
 * tab shows that plugin in BOTH lists. Rendering it in both sections would
 * contradict itself ("ok" + Disable up top, "disabled" + Enable below), so
 * any name present in both is rendered ONCE, in the health section, with a
 * "disable pending" badge -- the bottom "Disabled plugins" section is only
 * for names that are `plugins_disabled` AND not currently loaded.
 */
export function HealthView() {
  const { can } = useRole()
  const canAdmin = can('admin')
  const health = useAsyncData(() => fetchPluginsHealth(), [])
  const [toggled, setToggled] = useState<Record<string, ToggleState>>({})

  function handleToggled(name: string, result: ToggleState) {
    setToggled((prev) => ({ ...prev, [name]: result }))
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Plugin health</CardTitle>
        <CardDescription>
          Process-global plugin status, same as `hivepilot plugins health`.
          {canAdmin && ' Enable/disable applies on the server’s next restart only.'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={health}
          isEmpty={(data) => data.plugins.length === 0 && data.disabled.length === 0}
          emptyMessage="No plugins registered."
        >
          {(data) => {
            const disabledNames = new Set(data.disabled)
            const loadedNames = new Set(data.plugins.map((plugin) => plugin.name))
            // A name in BOTH sets is loaded now but flagged in
            // `plugins_disabled` -- it renders once, above, in the health
            // list (with a "disable pending" badge), so exclude it here.
            const trulyDisabled = data.disabled.filter((name) => !loadedNames.has(name))

            return (
              <div className="flex flex-col gap-4">
                <ul className="flex flex-col gap-2">
                  {data.plugins.map((plugin) => {
                    // Seeded from the load-time snapshot; a local toggle this
                    // session (`toggled[plugin.name]`) always overrides it, both
                    // for the button label (via `PluginToggle`'s own precedence)
                    // and for the badge below.
                    const pendingFromLoad = disabledNames.has(plugin.name)
                    const pendingDisable = toggled[plugin.name]
                      ? toggled[plugin.name].disabled
                      : pendingFromLoad

                    return (
                      <li
                        key={plugin.name}
                        className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2"
                      >
                        <span className="font-medium">{plugin.name}</span>
                        <Badge variant={STATUS_VARIANT[plugin.status]}>{plugin.status}</Badge>
                        {pendingDisable && (
                          <Badge
                            variant="outline"
                            title="Flagged to disable — takes effect on the server's next restart. Currently still active."
                          >
                            disable pending · restart
                          </Badge>
                        )}
                        {plugin.detail && (
                          <span className="text-sm text-muted-foreground">{plugin.detail}</span>
                        )}
                        {canAdmin && (
                          <PluginToggle
                            name={plugin.name}
                            toggled={toggled[plugin.name]}
                            onToggled={handleToggled}
                            initialDisabled={pendingFromLoad}
                          />
                        )}
                      </li>
                    )
                  })}
                </ul>
                {trulyDisabled.length > 0 && (
                  <div className="flex flex-col gap-2 border-t border-border pt-4">
                    <h3 className="text-sm font-semibold text-muted-foreground">
                      Disabled plugins
                    </h3>
                    <ul className="flex flex-col gap-2">
                      {trulyDisabled.map((name) => (
                        <li
                          key={name}
                          className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-border bg-muted/30 p-2"
                        >
                          <span className="font-medium">{name}</span>
                          <Badge variant="outline">disabled</Badge>
                          {canAdmin && (
                            <PluginToggle
                              name={name}
                              toggled={toggled[name]}
                              onToggled={handleToggled}
                              initialDisabled
                            />
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )
          }}
        </AsyncSection>
      </CardContent>
    </Card>
  )
}
