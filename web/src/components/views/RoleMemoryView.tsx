import { type FormEvent, useEffect, useState } from 'react'
import { Brain, Quote } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { ApiForbiddenError } from '@/lib/api'
import { useT } from '@/lib/i18n'
import {
  createHindsightMentalModel,
  curateHindsightMemory,
  fetchHindsightRolePanel,
  fetchHindsightStatus,
  refreshHindsightMentalModel,
  updateHindsightMentalModel,
  type HindsightMentalModel,
  type HindsightObservation,
  type HindsightRolePanel,
  type HindsightStatusResponse,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { AsyncSection } from './AsyncSection'

/**
 * HP-55 — per-role Hindsight bank (`role:{name}`): Mental Models +
 * Observations with quotes / proof count / optional confidence.
 *
 * Free text from Hindsight is UNTRUSTED — plain JSX only.
 * Observations are derived; human edits go to listed source facts.
 */
export function RoleMemoryView() {
  const t = useT()
  const { can } = useRole()
  const canEdit = can('run')
  const [role, setRole] = useState('')
  const [reload, setReload] = useState(0)
  const status = useAsyncData(() => fetchHindsightStatus(), [])

  useEffect(() => {
    if (role || status.status !== 'success') return
    const first = status.data.roles[0]?.name
    if (first) setRole(first)
  }, [role, status])

  const panel = useAsyncData<HindsightRolePanel | null>(
    () => (role ? fetchHindsightRolePanel(role) : Promise.resolve(null)),
    [role, reload],
  )

  if (status.status === 'error' && status.error instanceof ApiForbiddenError) {
    return (
      <div
        data-testid="role-memory-forbidden"
        className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
      >
        {t('quality.requiresTokenLead')}{' '}
        <span className="font-medium text-foreground">{t('quality.requiresTokenTail')}</span>{' '}
        {t('quality.requiresTokenNote')}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <AsyncSection state={status} isEmpty={(data) => data.roles.length === 0}>
        {(data) => (
          <RolePicker
            roles={data.roles}
            role={role}
            onChange={setRole}
            configured={data.configured}
            detail={data.detail}
          />
        )}
      </AsyncSection>

      {role ? (
        panel.status === 'error' && panel.error instanceof ApiForbiddenError ? (
          <div
            data-testid="role-memory-panel-forbidden"
            className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
          >
            {t('quality.requiresTokenLead')}{' '}
            <span className="font-medium text-foreground">{t('quality.requiresTokenTail')}</span>{' '}
            {t('quality.requiresTokenNote')}
          </div>
        ) : (
          <AsyncSection state={panel} isEmpty={() => false}>
            {(data) =>
              data == null ? null : (
                <RoleBankBody
                  panel={data}
                  canEdit={canEdit}
                  onChanged={() => setReload((n) => n + 1)}
                />
              )
            }
          </AsyncSection>
        )
      ) : null}
    </div>
  )
}

function RolePicker({
  roles,
  role,
  onChange,
  configured,
  detail,
}: {
  roles: HindsightStatusResponse['roles']
  role: string
  onChange: (name: string) => void
  configured: boolean
  detail?: string
}) {
  const t = useT()
  return (
    <div className="flex flex-col gap-2">
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">{t('memory.roleLabel')}</span>
        <Select
          data-testid="role-memory-select"
          aria-label={t('memory.roleLabel')}
          value={role}
          onChange={(event) => onChange(event.target.value)}
        >
          {roles.map((item) => (
            <option key={item.name} value={item.name}>
              {item.display_name ? `${item.display_name} (${item.name})` : item.name}
            </option>
          ))}
        </Select>
      </label>
      {role ? (
        <p data-testid="role-memory-bank" className="text-xs text-muted-foreground">
          {t('memory.bankId')}: {roles.find((item) => item.name === role)?.bank_id ?? `role:${role}`}
        </p>
      ) : null}
      {!configured && detail ? (
        <p data-testid="role-memory-unconfigured" className="text-sm text-muted-foreground">
          {detail}
        </p>
      ) : null}
    </div>
  )
}

function RoleBankBody({
  panel,
  canEdit,
  onChanged,
}: {
  panel: HindsightRolePanel
  canEdit: boolean
  onChanged: () => void
}) {
  const t = useT()
  if (!panel.configured) {
    return (
      <EmptyState
        data-testid="role-memory-disabled"
        icon={<Brain className="size-4" />}
        title={t('memory.knowledgeDisabledTitle')}
        body={panel.detail ?? t('memory.knowledgeDisabledBody')}
        className="max-w-2xl"
      />
    )
  }

  return (
    <div className="flex flex-col gap-6">
      {panel.detail ? (
        <p className="text-sm text-muted-foreground">{panel.detail}</p>
      ) : null}
      <MentalModelsSection
        role={panel.role}
        models={panel.mental_models}
        canEdit={canEdit}
        onChanged={onChanged}
      />
      <ObservationsSection
        role={panel.role}
        observations={panel.observations}
        canEdit={canEdit}
        onChanged={onChanged}
      />
    </div>
  )
}

function MentalModelsSection({
  role,
  models,
  canEdit,
  onChanged,
}: {
  role: string
  models: HindsightMentalModel[]
  canEdit: boolean
  onChanged: () => void
}) {
  const t = useT()
  return (
    <section className="flex flex-col gap-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Brain className="size-4" />
        {t('memory.mentalModelsTitle')}
      </h3>
      <p className="text-sm text-muted-foreground">{t('memory.mentalModelsHelp')}</p>
      {models.length === 0 ? (
        <p data-testid="role-memory-models-empty" className="text-sm text-muted-foreground">
          {t('memory.mentalModelsEmpty')}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {models.map((model) => (
            <MentalModelCard
              key={model.id || model.name}
              role={role}
              model={model}
              canEdit={canEdit}
              onChanged={onChanged}
            />
          ))}
        </div>
      )}
      {canEdit ? <CreateMentalModelForm role={role} onChanged={onChanged} /> : null}
    </section>
  )
}

function MentalModelCard({
  role,
  model,
  canEdit,
  onChanged,
}: {
  role: string
  model: HindsightMentalModel
  canEdit: boolean
  onChanged: () => void
}) {
  const t = useT()
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(model.name)
  const [query, setQuery] = useState(model.source_query)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await updateHindsightMentalModel(role, model.id, { name, source_query: query })
      setEditing(false)
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function refresh() {
    setBusy(true)
    setError(null)
    try {
      await refreshHindsightMentalModel(role, model.id)
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card data-testid="role-memory-model">
      <CardHeader className="gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-sm">{model.name}</CardTitle>
          {model.is_stale ? <Badge variant="outline">{t('memory.stale')}</Badge> : null}
        </div>
        <CardDescription>{model.source_query}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {model.content ? (
          <p className="whitespace-pre-wrap text-sm">{model.content}</p>
        ) : (
          <p className="text-sm text-muted-foreground">{t('memory.mentalModelNoContent')}</p>
        )}
        {canEdit && editing ? (
          <div className="flex flex-col gap-2">
            <Input
              aria-label={t('memory.mentalModelName')}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
            <textarea
              aria-label={t('memory.mentalModelQuery')}
              className="min-h-20 w-full rounded-lg border border-input bg-transparent px-2.5 py-1 text-sm"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" disabled={busy} onClick={() => void save()}>
                {t('memory.save')}
              </Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>
                {t('memory.cancel')}
              </Button>
            </div>
          </div>
        ) : canEdit ? (
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(true)}>
              {t('memory.edit')}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void refresh()}>
              {t('memory.refresh')}
            </Button>
          </div>
        ) : null}
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}

function CreateMentalModelForm({ role, onChanged }: { role: string; onChanged: () => void }) {
  const t = useT()
  const [name, setName] = useState('')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim() || !query.trim()) return
    setBusy(true)
    setError(null)
    try {
      await createHindsightMentalModel(role, { name: name.trim(), source_query: query.trim() })
      setName('')
      setQuery('')
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      data-testid="role-memory-create-model"
      className="flex flex-col gap-2 rounded-lg border border-border p-3"
      onSubmit={(event) => void onSubmit(event)}
    >
      <p className="text-sm font-medium">{t('memory.createMentalModel')}</p>
      <Input
        aria-label={t('memory.mentalModelName')}
        placeholder={t('memory.mentalModelName')}
        value={name}
        onChange={(event) => setName(event.target.value)}
      />
      <textarea
        aria-label={t('memory.mentalModelQuery')}
        placeholder={t('memory.mentalModelQuery')}
        className="min-h-20 w-full rounded-lg border border-input bg-transparent px-2.5 py-1 text-sm"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      <Button type="submit" size="sm" disabled={busy || !name.trim() || !query.trim()}>
        {t('memory.create')}
      </Button>
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </form>
  )
}

function ObservationsSection({
  role,
  observations,
  canEdit,
  onChanged,
}: {
  role: string
  observations: HindsightObservation[]
  canEdit: boolean
  onChanged: () => void
}) {
  const t = useT()
  return (
    <section className="flex flex-col gap-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Quote className="size-4" />
        {t('memory.observationsTitle')}
      </h3>
      <p className="text-sm text-muted-foreground">{t('memory.observationsHelp')}</p>
      {observations.length === 0 ? (
        <p data-testid="role-memory-observations-empty" className="text-sm text-muted-foreground">
          {t('memory.observationsEmpty')}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {observations.map((item) => (
            <ObservationCard
              key={item.id || item.text}
              role={role}
              observation={item}
              canEdit={canEdit}
              onChanged={onChanged}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function ObservationCard({
  role,
  observation,
  canEdit,
  onChanged,
}: {
  role: string
  observation: HindsightObservation
  canEdit: boolean
  onChanged: () => void
}) {
  const t = useT()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const confidence =
    observation.confidence == null ? null : `${Math.round(observation.confidence * 100)}%`

  async function saveFact(memoryId: string) {
    setBusy(true)
    setError(null)
    try {
      await curateHindsightMemory(role, memoryId, {
        text: draft.trim(),
        reason: reason.trim() || undefined,
      })
      setEditingId(null)
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card data-testid="role-memory-observation">
      <CardHeader className="gap-1">
        <CardTitle className="text-sm">{observation.text}</CardTitle>
        <CardDescription className="flex flex-wrap gap-2">
          <span>
            {t('memory.proofCount')}: {observation.proof_count}
          </span>
          {confidence ? (
            <span>
              {t('memory.confidence')}: {confidence}
            </span>
          ) : (
            <span>{t('memory.confidenceUnknown')}</span>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {observation.quotes.length > 0 ? (
          <ul className="flex flex-col gap-1">
            {observation.quotes.map((quote) => (
              <li key={`${quote.source_id}:${quote.text}`} className="text-sm text-muted-foreground">
                “{quote.text}”
              </li>
            ))}
          </ul>
        ) : null}
        {canEdit && observation.evidence.length > 0 ? (
          <div className="flex flex-col gap-2">
            <p className="text-xs text-muted-foreground">{t('memory.editSourceFactHelp')}</p>
            {observation.evidence
              .filter((fact) => fact.id)
              .map((fact) => (
                <div key={fact.id} className="flex flex-col gap-2 rounded-md border border-border p-2">
                  <p className="text-sm">{fact.text}</p>
                  {editingId === fact.id ? (
                    <>
                      <textarea
                        aria-label={t('memory.sourceFactText')}
                        className="min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-1 text-sm"
                        value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                      />
                      <Input
                        aria-label={t('memory.editReason')}
                        placeholder={t('memory.editReason')}
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                      />
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          disabled={busy || !draft.trim()}
                          onClick={() => void saveFact(fact.id)}
                        >
                          {t('memory.save')}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => setEditingId(null)}
                        >
                          {t('memory.cancel')}
                        </Button>
                      </div>
                    </>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setEditingId(fact.id)
                        setDraft(fact.text)
                        setReason('')
                      }}
                    >
                      {t('memory.editSourceFact')}
                    </Button>
                  )}
                </div>
              ))}
          </div>
        ) : null}
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}
