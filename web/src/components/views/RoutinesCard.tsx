import { Repeat } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { formatAge } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  createRoutine,
  deleteRoutine,
  fetchRoles,
  fetchRoutines,
  patchRoutine,
  triggerRoutine,
  type Routine,
  type StudioRole,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'

const EMPTY_FORM = {
  role: 'developer',
  crons: '0 9 * * 1',
  timezone: 'UTC',
  projects: '',
  replace_key: '',
}

export function RoutinesCard() {
  const t = useT()
  const { can } = useRole()
  const canRun = can('run')
  const [refreshKey, setRefreshKey] = useState(0)
  const routinesState = useAsyncData(() => fetchRoutines(), [refreshKey])
  const rolesState = useAsyncData(() => fetchRoles(), [])
  const forbidden =
    routinesState.status === 'error' && routinesState.error instanceof ApiForbiddenError

  if (forbidden) return null

  const roles = rolesState.status === 'success' ? rolesState.data.roles : []

  return (
    <section className="flex flex-col gap-3" data-testid="routines-card">
      <h3 className="text-sm font-semibold tracking-wide">{t('routines.title')}</h3>
      <p className="text-sm text-muted-foreground">{t('routines.subtitle')}</p>
      {canRun ? (
        <RoutineForm
          roles={roles}
          onCreated={() => setRefreshKey((k) => k + 1)}
        />
      ) : (
        <p className="text-xs text-muted-foreground">{t('routines.needRun')}</p>
      )}
      {routinesState.status === 'success' ? (
        <RoutineList
          routines={routinesState.data.routines}
          canRun={canRun}
          onChanged={() => setRefreshKey((k) => k + 1)}
        />
      ) : routinesState.status === 'error' ? (
        <p className="text-sm text-destructive">{describeApiError(routinesState.error)}</p>
      ) : (
        <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
      )}
    </section>
  )
}

function RoutineForm({
  roles,
  onCreated,
}: {
  roles: StudioRole[]
  onCreated: () => void
}) {
  const t = useT()
  const [form, setForm] = useState(EMPTY_FORM)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const options = useMemo(
    () => roles.filter((role) => Boolean(role.command_task)),
    [roles],
  )

  async function submit() {
    setPending(true)
    setError(null)
    try {
      const crons = form.crons
        .split(/\n|,/)
        .map((item) => item.trim())
        .filter(Boolean)
      const projects = form.projects
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean)
      await createRoutine({
        role: form.role.trim(),
        crons,
        timezone: form.timezone.trim() || 'UTC',
        projects,
        enabled: true,
        replace_key: form.replace_key.trim() || null,
      })
      setForm({ ...EMPTY_FORM, role: form.role })
      onCreated()
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <form
      data-testid="routine-form"
      className="grid grid-cols-1 gap-2 rounded-lg border border-border p-3 md:grid-cols-2"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <Field label={t('routines.fieldRole')}>
        {options.length > 0 ? (
          <select
            data-testid="routine-field-role"
            className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            value={form.role}
            onChange={(event) => setForm({ ...form, role: event.target.value })}
          >
            {options.map((role) => (
              <option key={role.name} value={role.name}>
                {role.display_name || role.title || role.name}
              </option>
            ))}
          </select>
        ) : (
          <Input
            data-testid="routine-field-role"
            value={form.role}
            onChange={(event) => setForm({ ...form, role: event.target.value })}
          />
        )}
      </Field>
      <Field label={t('routines.fieldTimezone')}>
        <Input
          data-testid="routine-field-timezone"
          value={form.timezone}
          onChange={(event) => setForm({ ...form, timezone: event.target.value })}
        />
      </Field>
      <Field label={t('routines.fieldCrons')}>
        <Input
          data-testid="routine-field-crons"
          value={form.crons}
          onChange={(event) => setForm({ ...form, crons: event.target.value })}
        />
      </Field>
      <Field label={t('routines.fieldProjects')}>
        <Input
          data-testid="routine-field-projects"
          value={form.projects}
          onChange={(event) => setForm({ ...form, projects: event.target.value })}
        />
      </Field>
      <Field label={t('routines.fieldReplaceKey')}>
        <Input
          data-testid="routine-field-replace-key"
          value={form.replace_key}
          onChange={(event) => setForm({ ...form, replace_key: event.target.value })}
        />
      </Field>
      <div className="flex items-end">
        <Button type="submit" size="sm" disabled={pending} data-testid="routine-save">
          {t('routines.save')}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="md:col-span-2 text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </form>
  )
}

function RoutineList({
  routines,
  canRun,
  onChanged,
}: {
  routines: Routine[]
  canRun: boolean
  onChanged: () => void
}) {
  const t = useT()
  if (routines.length === 0) {
    return (
      <EmptyState
        data-testid="routines-empty"
        icon={<Repeat className="size-4" />}
        title={t('routines.emptyTitle')}
        body={t('routines.emptyBody')}
      />
    )
  }
  return (
    <ul className="flex flex-col gap-2" data-testid="routines-list">
      {routines.map((row) => (
        <RoutineRow key={row.id} row={row} canRun={canRun} onChanged={onChanged} />
      ))}
    </ul>
  )
}

function RoutineRow({
  row,
  canRun,
  onChanged,
}: {
  row: Routine
  canRun: boolean
  onChanged: () => void
}) {
  const t = useT()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setPending(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <li
      data-testid={`routine-${row.replace_key || row.id}`}
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm"
    >
      <span className="font-medium">{row.role}</span>
      <span className="text-muted-foreground">{row.crons.join(', ')}</span>
      <Badge variant="outline">{row.timezone}</Badge>
      {!row.enabled && <Badge variant="secondary">{t('routines.disabled')}</Badge>}
      {row.replace_key ? <Badge variant="outline">{row.replace_key}</Badge> : null}
      <span className="metric-mono text-xs text-muted-foreground">
        {row.next_run_at
          ? t('routines.nextRun', { age: formatAge(row.next_run_at) })
          : t('routines.noNext')}
      </span>
      <span className="metric-mono text-xs text-muted-foreground">
        {row.last_run_at
          ? t('routines.lastRun', { age: formatAge(row.last_run_at) })
          : t('routines.neverRun')}
      </span>
      <div className="ml-auto flex flex-wrap gap-1">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!canRun || pending}
          onClick={() => void run(() => triggerRoutine(row.id))}
        >
          {t('routines.trigger')}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!canRun || pending}
          onClick={() => void run(() => patchRoutine(row.id, { enabled: !row.enabled }))}
        >
          {row.enabled ? t('routines.disable') : t('routines.enable')}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={!canRun || pending}
          onClick={() => {
            if (!window.confirm(t('routines.deleteConfirm', { role: row.role }))) return
            void run(() => deleteRoutine(row.id))
          }}
        >
          {t('routines.delete')}
        </Button>
      </div>
      {error ? <p className="w-full text-xs text-destructive">{error}</p> : null}
    </li>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}
