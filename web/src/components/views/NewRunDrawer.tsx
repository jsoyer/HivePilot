import { type FormEvent, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Drawer } from '@/components/ui/drawer'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import { createRun, fetchProjectNames, fetchTaskNames } from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'

interface CatalogueFieldProps {
  id: string
  label: string
  /** `null` while the catalogue is still loading, or once it failed. */
  options: string[] | null
  placeholder: string
  chooseLabel: string
  fallbackNote: string
  value: string
  onChange: (value: string) => void
  disabled: boolean
}

/**
 * One required "pick a server-known value" field.
 *
 * The values `POST /v1/runs` accepts for `project` and `task` are declared in
 * config and enumerable over `GET /v1/projects` / `GET /v1/tasks`. Asking an
 * operator to TYPE them (the old form's `e.g. deploy` / `e.g. acme-web`
 * placeholders) is a guessing game that ends in a 4xx.
 *
 * The free-text fallback is deliberate and must not be removed: if the
 * catalogue endpoint fails, or a deployment genuinely has none of one kind
 * registered yet, an operator with a `run` token must still be able to
 * trigger a run. A pick-list that can lock you out is worse than a text box.
 */
function CatalogueField({
  id,
  label,
  options,
  placeholder,
  chooseLabel,
  fallbackNote,
  value,
  onChange,
  disabled,
}: CatalogueFieldProps) {
  const enumerable = options !== null && options.length > 0

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {enumerable ? (
        <Select
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled}
          required
        >
          <option value="">{chooseLabel}</option>
          {options.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </Select>
      ) : (
        <>
          <Input
            id={id}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder={placeholder}
            disabled={disabled}
            required
          />
          <p data-testid={`${id}-fallback`} className="text-xs text-muted-foreground">
            {fallbackNote}
          </p>
        </>
      )}
    </div>
  )
}

export interface NewRunDrawerProps {
  /** Called after `POST /v1/runs` returns 202 — the board refreshes at once
   * rather than waiting for the next poll tick. */
  onCreated: () => void
  onClose: () => void
}

/**
 * "New run" as a SECONDARY action in a drawer, not a form nailed to the top
 * of the board.
 *
 * The board's job is to show what is happening; creating a run is something
 * an operator does occasionally, from a button. The previous layout gave a
 * permanently-open form the top third of the Runs view, above any content.
 *
 * `POST /v1/runs` is asynchronous (202 + background execution server-side),
 * so submission resolves fast regardless of how long the run itself takes.
 * The `run` role is enforced server-side; the caller only renders this
 * drawer for a `run`-rank token, and a stray 403 still surfaces inline.
 */
export function NewRunDrawer({ onCreated, onClose }: NewRunDrawerProps) {
  const t = useT()
  const [task, setTask] = useState('')
  const [project, setProject] = useState('')
  const [extraPrompt, setExtraPrompt] = useState('')
  const [autoGit, setAutoGit] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A catalogue that fails to load resolves to `null`, never to a throw that
  // would blank the drawer — `CatalogueField` degrades to free text.
  const projectsState = useAsyncData<string[] | null>(() => fetchProjectNames().catch(() => null), [])
  const tasksState = useAsyncData<string[] | null>(() => fetchTaskNames().catch(() => null), [])
  const projects = projectsState.status === 'success' ? projectsState.data : null
  const tasks = tasksState.status === 'success' ? tasksState.data : null

  const canSubmit = task.trim().length > 0 && project.trim().length > 0 && !submitting

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createRun({
        task: task.trim(),
        project: project.trim(),
        extra_prompt: extraPrompt.trim() ? extraPrompt.trim() : undefined,
        auto_git: autoGit,
      })
      onCreated()
      onClose()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('runs.insufficientRoleCreate') : describeApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Drawer title={t('runs.newRunTitle')} closeLabel={t('runs.newRunCloseAriaLabel')} onClose={onClose}>
      <p className="text-sm text-muted-foreground">{t('runs.newRunHelp')}</p>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <CatalogueField
          id="new-run-project"
          label={t('common.project')}
          options={projects}
          placeholder={t('runs.projectPlaceholder')}
          chooseLabel={t('runs.chooseProject')}
          fallbackNote={t('runs.projectCatalogueUnavailable')}
          value={project}
          onChange={setProject}
          disabled={submitting}
        />
        <CatalogueField
          id="new-run-task"
          label={t('common.task')}
          options={tasks}
          placeholder={t('runs.taskPlaceholder')}
          chooseLabel={t('runs.chooseTask')}
          fallbackNote={t('runs.taskCatalogueUnavailable')}
          value={task}
          onChange={setTask}
          disabled={submitting}
        />

        <div className="flex flex-col gap-1">
          <label htmlFor="new-run-extra-prompt" className="text-sm font-medium">
            {t('runs.extraPromptLabel')}
          </label>
          <textarea
            id="new-run-extra-prompt"
            value={extraPrompt}
            onChange={(event) => setExtraPrompt(event.target.value)}
            placeholder={t('runs.extraPromptPlaceholder')}
            disabled={submitting}
            rows={4}
            className="w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
          />
        </div>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={autoGit}
            onChange={(event) => setAutoGit(event.target.checked)}
            disabled={submitting}
            className="size-4 rounded border-input"
          />
          {t('runs.autoGitLabel')}
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={!canSubmit}>
            {submitting ? t('common.starting') : t('runs.newRunButton')}
          </Button>
          <Button type="button" variant="ghost" onClick={onClose} disabled={submitting}>
            {t('common.cancel')}
          </Button>
        </div>

        {error && (
          <div role="alert" className="text-sm text-destructive">
            {error}
          </div>
        )}
      </form>
    </Drawer>
  )
}
