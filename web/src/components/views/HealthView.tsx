import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import { formatAge, formatTimestamp } from '@/lib/format-time'
import {
  fetchHealthProbes,
  fetchPluginsHealth,
  togglePlugin,
  type PluginHealthEntry,
  type PluginHealthStatus,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const STATUS_VARIANT: Record<PluginHealthStatus, 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  degraded: 'outline',
  error: 'destructive',
}

/**
 * What the activity probe found, as five mutually exclusive states.
 *
 * Modelling this as a union rather than a pile of nullable fields is
 * deliberate: the states that matter here are the ones easiest to collapse by
 * accident. `never` ("measured, and it has done nothing") is a real reading;
 * `presenceOnly` ("nothing records this plugin's use") is the absence of one;
 * `unreadable` ("measurable, but the read failed") is a third thing again.
 * Rendering any of them as "0" would tell the operator something untrue.
 */
type ActivityState =
  | { kind: 'active'; events: number; lastUsed: string; evidence: string }
  | { kind: 'idle'; windowDays: number; lastUsed: string; evidence: string }
  | { kind: 'never'; evidence: string }
  | { kind: 'unreadable' }
  | { kind: 'presenceOnly' }

function activityState(plugin: PluginHealthEntry): ActivityState {
  if (!plugin.activity_available) return { kind: 'presenceOnly' }
  if (!plugin.activity) return { kind: 'unreadable' }

  const { events, last_used: lastUsed, window_days: windowDays, evidence } = plugin.activity
  // No timestamp means nothing was ever recorded. Checked before `events` so
  // a count without a date can never render as an activity with no date.
  if (!lastUsed) return { kind: 'never', evidence }
  if (events > 0) return { kind: 'active', events, lastUsed, evidence }
  return { kind: 'idle', windowDays, lastUsed, evidence }
}

/** A plugin that loads, reports `ok`, and has never once run.
 *
 * This is the exact state `headroom` and `mem0` sat in for weeks, and the
 * reason this view was rebuilt: nothing on screen contradicted the green
 * badge. It gets its own marker so it cannot pass for healthy again. */
function contradictsStatus(plugin: PluginHealthEntry, activity: ActivityState): boolean {
  return plugin.status === 'ok' && activity.kind === 'never'
}

/** Per-row toggle result, tracked locally so a just-toggled row can show a
 * "restart required" badge immediately without waiting for (or requiring) a
 * live-reloading `GET /v1/plugins/health` -- the backend never live-applies
 * a toggle (see `togglePlugin`'s docstring in `@/lib/pollen-api`), so this
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
  const t = useT()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleClick() {
    setSubmitting(true)
    setError(null)
    try {
      const result = await togglePlugin(name)
      onToggled(name, { disabled: result.disabled, restartRequired: result.restart_required })
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('health.insufficientRole') : describeApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  const isDisabled = toggled?.disabled ?? initialDisabled
  const actionLabel = isDisabled ? t('common.enable') : t('common.disable')

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
          aria-label={`${actionLabel} ${name}`}
          title={t('health.restartTakesEffectTitle')}
        >
          {submitting ? t('common.working') : actionLabel}
        </Button>
        {toggled?.restartRequired && (
          <Badge variant="outline" title={t('health.restartAppliesTitle')}>
            {t('health.restartRequired')}
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

/** Colour carries emphasis, the leading glyph carries the state.
 *
 * Encoding activity in hue alone would leave the whole distinction invisible
 * to a colour-blind operator and to anyone reading a greyscale screenshot —
 * and this column exists precisely to be noticed. */
const ACTIVITY_STYLE: Record<ActivityState['kind'], { glyph: string; className: string }> = {
  active: { glyph: '●', className: 'text-emerald-600 dark:text-emerald-500' },
  idle: { glyph: '◐', className: 'text-muted-foreground' },
  never: { glyph: '○', className: 'text-amber-600 dark:text-amber-500' },
  unreadable: { glyph: '?', className: 'text-muted-foreground' },
  presenceOnly: { glyph: '–', className: 'text-muted-foreground/70' },
}

function ActivityChip({ state }: { state: ActivityState }) {
  const t = useT()
  const style = ACTIVITY_STYLE[state.kind]

  let label: string
  let title: string

  switch (state.kind) {
    case 'active':
      label = t('health.activity.active', {
        count: state.events,
        when: formatAge(state.lastUsed),
      })
      title = `${formatTimestamp(state.lastUsed)} — ${t('health.activity.evidenceTitle', { evidence: state.evidence })}`
      break
    case 'idle':
      label = t('health.activity.idle', {
        days: state.windowDays,
        when: formatAge(state.lastUsed),
      })
      title = `${formatTimestamp(state.lastUsed)} — ${t('health.activity.evidenceTitle', { evidence: state.evidence })}`
      break
    case 'never':
      label = t('health.activity.neverRun')
      title = t('health.activity.evidenceTitle', { evidence: state.evidence })
      break
    case 'unreadable':
      label = t('health.activity.unreadable')
      title = t('health.activity.unreadableTitle')
      break
    case 'presenceOnly':
      label = t('health.activity.presenceOnly')
      title = t('health.activity.presenceOnlyTitle')
      break
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 text-sm tabular-nums ${style.className}`}
      title={title}
    >
      <span aria-hidden="true">{style.glyph}</span>
      {label}
    </span>
  )
}

/**
 * Health tab -- `GET /v1/plugins/health`, one badge per plugin, plus an
 * admin-only enable/disable toggle (`POST /v1/plugins/{name}/toggle`,
 * Mirador actionable dashboard PRD, Sprint 5).
 *
 * **Two independent answers per plugin.** `status` says installed and
 * configured; `activity` says it has actually run. They can disagree for a
 * long time without anything looking wrong — `headroom` and `mem0` both held
 * `ok` for weeks while failing every call. The activity column, the summary
 * counts, and the `contradictsStatus` marker all exist so that state is
 * visible on screen instead of having to be discovered in the database.
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

/**
 * Two probes for the things that fail by GOING QUIET.
 *
 * The plugin table below reports what LOADED. These report whether two systems
 * that produce continuously are still producing — the failure mode nothing
 * else on this page can see, because an absence looks exactly like a healthy
 * zero.
 *
 * `not_configured` is deliberately NOT a fault. A red badge on every
 * deployment that never asked for a live agent surface teaches people to
 * ignore the badge, and the badge that matters is `unreachable`.
 *
 * `never_arrived` and `stale` are likewise different answers: the first points
 * at configuration, the second at an exporter that used to work and stopped.
 */
function SystemProbes() {
  const t = useT()
  // Isolated on purpose. A probe panel must never be able to take down the
  // page it observes -- and it nearly did: an endpoint this view had not seen
  // before made `useAsyncData` call `.then` on an undefined, and the whole
  // plugin table went with it. Health surfaces that can break the thing they
  // report on are worse than no surface.
  const probes = useAsyncData(
    () =>
      Promise.resolve()
        .then(() => fetchHealthProbes())
        .catch(() => null),
    [],
  )

  return (
    <AsyncSection state={probes} isEmpty={(d) => !d}>
      {(data) =>
        data ? (
        <div data-testid="health-probes" className="mb-6 flex flex-wrap gap-2">
          <Badge
            data-testid="health-probe-surface"
            variant={data.agent_surface.state === 'unreachable' ? 'destructive' : 'outline'}
          >
            {t(`health.probeSurface.${data.agent_surface.state}`, {
              defaultValue: data.agent_surface.state,
              backend: data.agent_surface.backend ?? '',
            })}
          </Badge>
          <Badge
            data-testid="health-probe-otel"
            variant={data.otel.state === 'stale' ? 'destructive' : 'outline'}
          >
            {t(`health.probeOtel.${data.otel.state}`, {
              defaultValue: data.otel.state,
              rows: data.otel.rows ?? 0,
              hours: data.otel.age_hours ?? 0,
            })}
          </Badge>
        </div>
        ) : null
      }
    </AsyncSection>
  )
}

export function HealthView() {
  const t = useT()
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
        <CardTitle>{t('health.title')}</CardTitle>
        <CardDescription>
          {t('health.description')}
          {t('health.activityNote')}
          {canAdmin && t('health.restartNote')}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <SystemProbes />
        <AsyncSection
          state={health}
          // `denied` and `not_installed` count as content. Without them, a
          // host where every plugin was rolled back at load would render
          // "No plugins registered" and hide the denial list — reproducing
          // the silence this section exists to end, in the one case where it
          // matters most.
          isEmpty={(data) =>
            data.plugins.length === 0 &&
            data.disabled.length === 0 &&
            (data.denied ?? []).length === 0 &&
            (data.not_installed ?? []).length === 0
          }
          emptyMessage={t('health.noPlugins')}
        >
          {(data) => {
            const disabledNames = new Set(data.disabled)
            const loadedNames = new Set(data.plugins.map((plugin) => plugin.name))
            // A name in BOTH sets is loaded now but flagged in
            // `plugins_disabled` -- it renders once, above, in the health
            // list (with a "disable pending" badge), so exclude it here.
            const trulyDisabled = data.disabled.filter((name) => !loadedNames.has(name))

            const states = data.plugins.map((plugin) => activityState(plugin))
            // Counted separately rather than rolled into one "healthy"
            // figure: a plugin nothing measures and a plugin measured as idle
            // are not interchangeable, and summing them would recreate the
            // false reassurance this view was rebuilt to remove.
            //
            // **Every state gets a bucket.** An earlier version counted only
            // exercised/never-run/presence-only, so a plugin idle for ninety
            // days — or one whose probe failed — fell through all three and
            // the strip still read "0 never run". A count that silently omits
            // a case is worse than no count: it reassures about ground it
            // never covered. `test_every_plugin_lands_in_exactly_one_bucket`
            // pins the totals to `plugins.length`.
            const count = (kind: ActivityState['kind']) =>
              states.filter((state) => state.kind === kind).length
            const exercised = count('active')
            const idle = count('idle')
            const neverRun = count('never')
            const unmeasured = count('presenceOnly')
            const unreadable = count('unreadable')

            return (
              <div className="flex flex-col gap-4">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm tabular-nums text-muted-foreground">
                  <span className="font-medium text-foreground">
                    {t('health.summary.loaded', { count: data.plugins.length })}
                  </span>
                  <span className={exercised > 0 ? 'text-emerald-600 dark:text-emerald-500' : ''}>
                    {ACTIVITY_STYLE.active.glyph}{' '}
                    {t('health.summary.exercised', { count: exercised })}
                  </span>
                  <span>
                    {ACTIVITY_STYLE.idle.glyph} {t('health.summary.idle', { count: idle })}
                  </span>
                  <span className={neverRun > 0 ? 'text-amber-600 dark:text-amber-500' : ''}>
                    {ACTIVITY_STYLE.never.glyph}{' '}
                    {t('health.summary.neverRun', { count: neverRun })}
                  </span>
                  <span>
                    {ACTIVITY_STYLE.presenceOnly.glyph}{' '}
                    {t('health.summary.unmeasured', { count: unmeasured })}
                  </span>
                  {/* An error state, not a resting state — shown only when it
                      actually happens rather than sitting at a permanent 0. */}
                  {unreadable > 0 && (
                    <span>
                      {ACTIVITY_STYLE.unreadable.glyph}{' '}
                      {t('health.summary.unreadable', { count: unreadable })}
                    </span>
                  )}
                </div>
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

                    const activity = activityState(plugin)
                    const contradicts = contradictsStatus(plugin, activity)

                    return (
                      <li
                        key={plugin.name}
                        className={`flex flex-col gap-2 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between ${
                          contradicts
                            ? 'border-amber-500/40 bg-amber-500/5'
                            : 'border-border'
                        }`}
                      >
                        <div className="flex min-w-0 flex-col gap-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium">{plugin.name}</span>
                            <Badge variant={STATUS_VARIANT[plugin.status]}>
                              {t(`health.status.${plugin.status}`)}
                            </Badge>
                            {pendingDisable && (
                              <Badge variant="outline" title={t('health.pendingBadgeTitle')}>
                                {t('health.disablePending')}
                              </Badge>
                            )}
                            {/* The badge above says `ok`. This says it has
                                never run. Both are true, and the operator
                                needs the second one to act on the first. */}
                            {contradicts && (
                              <Badge
                                variant="outline"
                                className="border-amber-500/50 text-amber-700 dark:text-amber-500"
                                title={t('health.activity.contradictionTitle')}
                              >
                                {t('health.activity.contradiction')}
                              </Badge>
                            )}
                          </div>
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                            <ActivityChip state={activity} />
                            {plugin.detail && (
                              <span className="truncate text-sm text-muted-foreground">
                                {plugin.detail}
                              </span>
                            )}
                          </div>
                        </div>
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
                {/* Enabled AND installed AND did not load — the third state,
                    which had no surface anywhere. `check_all()` only covers
                    REGISTERED plugins and a capability-denied plugin is rolled
                    back before registration, so it appears in neither the list
                    above nor the disabled list below. An operator could enable
                    it, watch the toggle succeed, and find it nowhere. */}
                {(data.denied ?? []).length > 0 && (
                  <div className="flex flex-col gap-2 border-t border-border pt-4">
                    <h3 className="text-sm font-semibold text-muted-foreground">
                      {t('health.deniedPlugins')}
                    </h3>
                    <ul className="flex flex-col gap-2">
                      {(data.denied ?? []).map((denied) => (
                        <li
                          key={denied.name}
                          className="flex flex-col gap-1 rounded-lg border border-destructive/40 bg-destructive/5 p-3"
                        >
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium">{denied.name}</span>
                            <Badge variant="destructive">{t('health.denied')}</Badge>
                          </div>
                          <span className="text-sm text-muted-foreground">{denied.error}</span>
                          <span className="text-sm text-muted-foreground">
                            {denied.remediation}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {/* Written in the repo, never fetched onto this host. Plugins
                    are not in the wheel, so a merge does not install them —
                    showing only what IS installed answers "what is on" while
                    hiding "what exists". Not a problem, so it is quiet: a
                    plain list, no badge, no severity. */}
                {(data.not_installed ?? []).length > 0 && (
                  <div className="flex flex-col gap-2 border-t border-border pt-4">
                    <h3 className="text-sm font-semibold text-muted-foreground">
                      {t('health.notInstalledPlugins')}
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      {t('health.notInstalledHint')}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {(data.not_installed ?? []).join(', ')}
                    </p>
                  </div>
                )}
                {trulyDisabled.length > 0 && (
                  <div className="flex flex-col gap-2 border-t border-border pt-4">
                    <h3 className="text-sm font-semibold text-muted-foreground">
                      {t('health.disabledPlugins')}
                    </h3>
                    <ul className="flex flex-col gap-2">
                      {trulyDisabled.map((name) => (
                        <li
                          key={name}
                          className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-border bg-muted/30 p-2"
                        >
                          <span className="font-medium">{name}</span>
                          <Badge variant="outline">{t('health.disabled')}</Badge>
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
