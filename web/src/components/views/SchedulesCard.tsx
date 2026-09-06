import { CalendarClock } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { SectionHeader } from '@/components/dashboard/SectionHeader'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { formatAge } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import { fetchSchedules, triggerSchedule, type ScheduleEntry } from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'

/**
 * Named schedules from `schedules.yaml` (HP-64). Lives on Autopilot because
 * those entries are what feed / drain the guarded queue.
 */
export function SchedulesCard() {
  const t = useT()
  const { can } = useRole()
  const canRun = can('run')
  const [refreshKey, setRefreshKey] = useState(0)
  const state = useAsyncData(() => fetchSchedules(), [refreshKey])
  const forbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  if (forbidden) return null

  return (
    <section className="flex flex-col gap-3" data-testid="schedules-card">
      <SectionHeader index="05" title={t('schedules.title')} />
      {state.status === 'success' ? (
        <ScheduleList
          schedules={state.data.schedules}
          canRun={canRun}
          onFired={() => setRefreshKey((k) => k + 1)}
        />
      ) : state.status === 'error' ? (
        <p className="text-sm text-destructive">{describeApiError(state.error)}</p>
      ) : (
        <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
      )}
    </section>
  )
}

function ScheduleList({
  schedules,
  canRun,
  onFired,
}: {
  schedules: ScheduleEntry[]
  canRun: boolean
  onFired: () => void
}) {
  const t = useT()
  if (schedules.length === 0) {
    return (
      <EmptyState
        data-testid="schedules-empty"
        icon={<CalendarClock className="size-4" />}
        title={t('schedules.emptyTitle')}
        body={t('schedules.emptyBody')}
      />
    )
  }
  return (
    <ul className="flex flex-col gap-2">
      {schedules.map((entry) => (
        <ScheduleRow key={entry.name} entry={entry} canRun={canRun} onFired={onFired} />
      ))}
    </ul>
  )
}

function ScheduleRow({
  entry,
  canRun,
  onFired,
}: {
  entry: ScheduleEntry
  canRun: boolean
  onFired: () => void
}) {
  const t = useT()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function fire() {
    if (!window.confirm(t('schedules.triggerConfirm', { name: entry.name }))) return
    setPending(true)
    setError(null)
    try {
      await triggerSchedule(entry.name)
      onFired()
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <li
      data-testid={`schedule-${entry.name}`}
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm"
    >
      <span className="font-medium">{entry.name}</span>
      <span className="text-muted-foreground">
        {entry.task ?? entry.source ?? ''}
        {(entry.projects ?? []).length ? ` · ${(entry.projects ?? []).join(', ')}` : ''}
      </span>
      <Badge variant="outline">{t('schedules.interval', { minutes: String(entry.interval_minutes) })}</Badge>
      {!entry.enabled && <Badge variant="secondary">{t('schedules.disabled')}</Badge>}
      <span className="metric-mono text-xs text-muted-foreground">
        {entry.last_run_at
          ? t('schedules.lastRun', { age: formatAge(entry.last_run_at) })
          : t('schedules.neverRun')}
      </span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="ml-auto"
        disabled={!canRun || pending || !entry.enabled}
        onClick={() => void fire()}
      >
        {t('schedules.trigger')}
      </Button>
      {error && <p className="w-full text-xs text-destructive">{error}</p>}
    </li>
  )
}
