import { useState, type ReactNode } from 'react'
import { RoleAvatar } from '@/components/RoleAvatar'
import { Button } from '@/components/ui/button'
import { Drawer } from '@/components/ui/drawer'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  createRole,
  deleteRole,
  fetchRoles,
  updateRole,
  type RoleWritePayload,
  type StudioRole,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * Agent Studio (HP-66): CRUD the store-backed roster on `/v1/roles`.
 * List/read for any token; create/update/delete hide unless `can('admin')`.
 */

function csv(values: string[] | null | undefined): string {
  return (values ?? []).join(', ')
}

function splitCsv(raw: string): string[] {
  return raw
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
}

function toPayload(form: RoleFormState): RoleWritePayload {
  const promptText = form.prompt_text.trim()
  const promptFile = form.prompt_file.trim()
  return {
    name: form.name.trim(),
    title: form.title.trim(),
    model_profile: form.model_profile.trim(),
    inputs: splitCsv(form.inputs),
    outputs: splitCsv(form.outputs),
    can_block: form.can_block,
    order: Number.parseInt(form.order, 10) || 0,
    display_name: form.display_name.trim() || null,
    runner: form.runner.trim() || null,
    model: form.model.trim() || null,
    prompt_text: promptText || null,
    prompt_file: promptText ? null : promptFile || null,
  }
}

interface RoleFormState {
  name: string
  title: string
  display_name: string
  model_profile: string
  model: string
  runner: string
  inputs: string
  outputs: string
  can_block: boolean
  order: string
  prompt_text: string
  prompt_file: string
}

function emptyForm(): RoleFormState {
  return {
    name: '',
    title: '',
    display_name: '',
    model_profile: 'architecture',
    model: '',
    runner: 'openai',
    inputs: '',
    outputs: '',
    can_block: false,
    order: '0',
    prompt_text: '',
    prompt_file: '',
  }
}

function formFromRole(role: StudioRole): RoleFormState {
  return {
    name: role.name,
    title: role.title,
    display_name: role.display_name ?? '',
    model_profile: role.model_profile,
    model: role.model ?? '',
    runner: role.runner ?? '',
    inputs: csv(role.inputs),
    outputs: csv(role.outputs),
    can_block: role.can_block,
    order: String(role.order ?? 0),
    prompt_text: role.prompt_text ?? '',
    prompt_file: role.prompt_file ?? '',
  }
}

type EditorMode = { kind: 'closed' } | { kind: 'create' } | { kind: 'edit'; role: StudioRole }

export function AgentStudioView() {
  const t = useT()
  const { can } = useRole()
  const canAdmin = can('admin')
  const [refreshKey, setRefreshKey] = useState(0)
  const rolesState = useAsyncData(fetchRoles, [refreshKey])
  const [editor, setEditor] = useState<EditorMode>({ kind: 'closed' })
  const [form, setForm] = useState<RoleFormState>(emptyForm())
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function openCreate() {
    setForm(emptyForm())
    setError(null)
    setEditor({ kind: 'create' })
  }

  function openEdit(role: StudioRole) {
    setForm(formFromRole(role))
    setError(null)
    setEditor({ kind: 'edit', role })
  }

  function closeEditor() {
    setEditor({ kind: 'closed' })
    setError(null)
  }

  async function save() {
    const payload = toPayload(form)
    if (!payload.name || !payload.title || !payload.model_profile) {
      setError(t('studio.missingRequired'))
      return
    }
    if (!payload.prompt_text && !payload.prompt_file) {
      setError(t('studio.needPrompt'))
      return
    }
    setWorking(true)
    setError(null)
    try {
      if (editor.kind === 'create') {
        await createRole(payload)
      } else if (editor.kind === 'edit') {
        await updateRole(editor.role.name, { ...payload, name: editor.role.name })
      }
      closeEditor()
      setRefreshKey((n) => n + 1)
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('studio.needAdmin') : describeApiError(err))
    } finally {
      setWorking(false)
    }
  }

  async function remove(role: StudioRole) {
    if (!window.confirm(t('studio.deleteConfirm', { name: role.display_name || role.title || role.name }))) {
      return
    }
    setWorking(true)
    setError(null)
    try {
      await deleteRole(role.name)
      closeEditor()
      setRefreshKey((n) => n + 1)
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('studio.needAdmin') : describeApiError(err))
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="flex flex-col gap-4" data-testid="agent-studio">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">{t('studio.title')}</h2>
          <p className="text-sm text-muted-foreground">{t('studio.subtitle')}</p>
        </div>
        {canAdmin && (
          <Button type="button" data-testid="studio-new" onClick={openCreate}>
            {t('studio.new')}
          </Button>
        )}
      </div>

      <AsyncSection
        state={rolesState}
        isEmpty={(data) => data.roles.length === 0}
        emptyMessage={t('studio.empty')}
      >
        {(data) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('studio.colRole')}</TableHead>
                <TableHead>{t('studio.colProfile')}</TableHead>
                <TableHead>{t('studio.colRunner')}</TableHead>
                <TableHead className="text-right">{t('studio.colOrder')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.roles.map((role) => (
                <TableRow
                  key={role.name}
                  data-testid={`studio-row-${role.name}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => openEdit(role)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      openEdit(role)
                    }
                  }}
                  className="cursor-pointer"
                >
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <RoleAvatar role={role.name} label={role.display_name || role.title} size={24} />
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate font-medium">{role.display_name || role.title}</span>
                        <span className="truncate text-xs text-muted-foreground">{role.name}</span>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>{role.model_profile}</TableCell>
                  <TableCell>{role.runner || '—'}</TableCell>
                  <TableCell className="text-right">{role.order}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </AsyncSection>

      {editor.kind !== 'closed' && (
        <Drawer
          title={editor.kind === 'create' ? t('studio.new') : editor.role.display_name || editor.role.title}
          ariaLabel={t('studio.editorAria')}
          closeLabel={t('studio.close')}
          onClose={closeEditor}
        >
          <RoleEditor
            form={form}
            setForm={setForm}
            nameLocked={editor.kind === 'edit'}
            canAdmin={canAdmin}
            working={working}
            error={error}
            onSave={save}
            onDelete={editor.kind === 'edit' ? () => remove(editor.role) : undefined}
          />
        </Drawer>
      )}
    </div>
  )
}

function Field({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

function RoleEditor({
  form,
  setForm,
  nameLocked,
  canAdmin,
  working,
  error,
  onSave,
  onDelete,
}: {
  form: RoleFormState
  setForm: (next: RoleFormState) => void
  nameLocked: boolean
  canAdmin: boolean
  working: boolean
  error: string | null
  onSave: () => void
  onDelete?: () => void
}) {
  const t = useT()
  const set = (patch: Partial<RoleFormState>) => setForm({ ...form, ...patch })
  const readOnly = !canAdmin

  return (
    <form
      data-testid="studio-editor"
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (canAdmin) onSave()
      }}
    >
      <Field label={t('studio.fieldName')}>
        <Input
          data-testid="studio-field-name"
          value={form.name}
          disabled={nameLocked || readOnly}
          onChange={(event) => set({ name: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldTitle')}>
        <Input
          data-testid="studio-field-title"
          value={form.title}
          disabled={readOnly}
          onChange={(event) => set({ title: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldDisplayName')}>
        <Input
          value={form.display_name}
          disabled={readOnly}
          onChange={(event) => set({ display_name: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldProfile')}>
        <Input
          value={form.model_profile}
          disabled={readOnly}
          onChange={(event) => set({ model_profile: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldRunner')}>
        <Input
          value={form.runner}
          disabled={readOnly}
          onChange={(event) => set({ runner: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldModel')}>
        <Input
          value={form.model}
          disabled={readOnly}
          onChange={(event) => set({ model: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldInputs')}>
        <Input
          value={form.inputs}
          disabled={readOnly}
          onChange={(event) => set({ inputs: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldOutputs')}>
        <Input
          value={form.outputs}
          disabled={readOnly}
          onChange={(event) => set({ outputs: event.target.value })}
        />
      </Field>
      <Field label={t('studio.fieldOrder')}>
        <Input
          type="number"
          value={form.order}
          disabled={readOnly}
          onChange={(event) => set({ order: event.target.value })}
        />
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          data-testid="studio-field-can-block"
          checked={form.can_block}
          disabled={readOnly}
          onChange={(event) => set({ can_block: event.target.checked })}
        />
        {t('studio.fieldCanBlock')}
      </label>
      <Field label={t('studio.fieldPrompt')}>
        <textarea
          data-testid="studio-field-prompt"
          value={form.prompt_text}
          disabled={readOnly}
          rows={6}
          onChange={(event) => set({ prompt_text: event.target.value })}
          className="min-h-24 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50"
        />
      </Field>
      {form.prompt_file && !form.prompt_text && (
        <p className="text-xs text-muted-foreground">{t('studio.promptFileHint', { file: form.prompt_file })}</p>
      )}
      {error && (
        <p role="alert" data-testid="studio-error" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {canAdmin && (
        <div className="flex flex-wrap gap-2">
          <Button type="submit" data-testid="studio-save" disabled={working}>
            {working ? t('common.working') : t('studio.save')}
          </Button>
          {onDelete && (
            <Button type="button" variant="outline" data-testid="studio-delete" disabled={working} onClick={onDelete}>
              {t('studio.delete')}
            </Button>
          )}
        </div>
      )}
      {!canAdmin && <p className="text-sm text-muted-foreground">{t('studio.readOnly')}</p>}
    </form>
  )
}
