import { type FormEvent, useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { cancelRun, createRun, fetchRuns, type RunSummary } from '@/lib/mirador-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'

/** Poll cadence for `GET /v1/runs` — status transitions (running ->
 * success/failed/pending) show up without a manual refresh. Must stay
 * `<= 3000` per the sprint's acceptance criteria. */
const POLL_INTERVAL_MS = 3000

/** `started_at`/`finished_at` are SQL `TIMESTAMP` strings (or `null` while
 * a run hasn't finished) — render in the viewer's locale, falling back to
 * the raw string if it doesn't parse, and an em dash while absent. */
function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function statusVariant(status: string): 'default' | 'secondary' | 'destructive' {
  if (status === 'success') return 'default'
  if (status === 'failed') return 'destructive'
  if (status === 'cancelled') return 'destructive'
  return 'secondary'
}

interface NewRunFormProps {
  onCreated: () => void
}

/**
 * New Run form — only rendered by the parent (`RunsView` below) when
 * `useRole().can('run')` (defense-in-depth; `POST /v1/runs` enforces the
 * same `run` role server-side regardless of what the client shows, see
 * `create_run` in `api_service.py`). Task/project are required client-side;
 * extra_prompt/auto_git are optional. `POST /v1/runs` is asynchronous — it
 * returns 202 immediately and the pipeline runs on a background thread
 * server-side — so submission resolves fast regardless of how long the
 * triggered run itself takes; `onCreated` forces an immediate list refresh
 * instead of waiting for the next poll tick.
 */
function NewRunForm({ onCreated }: NewRunFormProps) {
  const [task, setTask] = useState('')
  const [project, setProject] = useState('')
  const [extraPrompt, setExtraPrompt] = useState('')
  const [autoGit, setAutoGit] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
      setTask('')
      setProject('')
      setExtraPrompt('')
      setAutoGit(false)
      onCreated()
    } catch (err) {
      setError(
        err instanceof ApiForbiddenError
          ? 'Insufficient role — your token can no longer trigger runs.'
          : describeApiError(err),
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mb-6 flex flex-col gap-3 border-b border-border pb-6">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor="new-run-task" className="text-sm font-medium">
            Task
          </label>
          <Input
            id="new-run-task"
            value={task}
            onChange={(event) => setTask(event.target.value)}
            placeholder="e.g. deploy"
            required
            disabled={submitting}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="new-run-project" className="text-sm font-medium">
            Project
          </label>
          <Input
            id="new-run-project"
            value={project}
            onChange={(event) => setProject(event.target.value)}
            placeholder="e.g. acme-web"
            required
            disabled={submitting}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new-run-extra-prompt" className="text-sm font-medium">
          Extra prompt (optional)
        </label>
        <textarea
          id="new-run-extra-prompt"
          value={extraPrompt}
          onChange={(event) => setExtraPrompt(event.target.value)}
          placeholder="Additional context for this run…"
          disabled={submitting}
          rows={3}
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
        Auto-commit/push git actions
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!canSubmit}>
          {submitting ? 'Starting…' : 'New Run'}
        </Button>
        {submitting && (
          <span role="status" className="text-sm text-muted-foreground">
            Starting…
          </span>
        )}
      </div>
      {error && (
        <div role="alert" className="text-sm text-destructive">
          {error}
        </div>
      )}
    </form>
  )
}

interface StopButtonProps {
  run: RunSummary
  onStopped: () => void
}

/**
 * Stop control for a single `status === 'running'` row (`POST /v1/runs/
 * {run_id}/cancel`) -- only rendered by the parent when `useRole().can('run')`
 * (defense-in-depth; the server enforces the same `run` role regardless of
 * what the client shows, see `cancel_run` in `api_service.py`). Requires
 * confirmation before sending the request. Cancellation is cooperative and
 * best-effort: the run resolves to `cancelled` at its NEXT step boundary, not
 * immediately -- this component doesn't wait for that, it relies on
 * `RunsView`'s existing poll loop (and an immediate `onStopped` refresh) to
 * surface the eventual status transition. A `409` (the run already reached a
 * terminal status between this row rendering and the click -- a race with the
 * poll loop, not a bug) surfaces as an inline error, never a crash.
 */
function StopButton({ run, onStopped }: StopButtonProps) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleStop() {
    if (!window.confirm(`Stop run #${run.id} (${run.task} on ${run.project})?`)) return
    setSubmitting(true)
    setError(null)
    try {
      await cancelRun(run.id)
      onStopped()
    } catch (err) {
      setError(
        err instanceof ApiForbiddenError
          ? 'Insufficient role — your token can no longer stop this run.'
          : describeApiError(err),
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <Button
        size="sm"
        variant="destructive"
        disabled={submitting}
        onClick={() => {
          void handleStop()
        }}
        aria-label={`Stop run ${run.id}`}
      >
        {submitting ? 'Stopping…' : 'Stop'}
      </Button>
      {error && (
        <div role="alert" className="text-sm text-destructive">
          {error}
        </div>
      )}
    </div>
  )
}

/**
 * Runs tab — `GET /v1/runs` (tenant-filtered for non-admin roles, see
 * `list_runs` in `api_service.py`), polled every `POLL_INTERVAL_MS` so
 * status transitions show up without a manual refresh. A New Run form
 * (`POST /v1/runs`, async — 202 + background execution) is shown only for
 * `useRole().can('run')` — the server enforces the same `run` role
 * regardless of what the client shows.
 *
 * `GET /v1/runs` itself requires a `run`-rank token (stricter than the
 * token gate's own `read` floor) — a plain `read` token 403s and sees a
 * graceful message, same pattern as `ApprovalsView`/`Mem0View`.
 *
 * Never renders `RunSummary.detail` (untrusted free text, same caveat as
 * `Approval.metadata` elsewhere in this app) — only the typed, structural
 * fields (id/project/task/status/started/finished).
 */
export function RunsView() {
  const { can } = useRole()
  const canRun = can('run')
  const [refreshKey, setRefreshKey] = useState(0)
  const state = useAsyncData(() => fetchRuns(), [refreshKey])

  // Poll on an interval, cleaned up on unmount (or before the next interval
  // is registered) so a stale timer from a previous mount never leaks.
  useEffect(() => {
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [])

  function handleCreated() {
    setRefreshKey((key) => key + 1)
  }

  function handleStopped() {
    setRefreshKey((key) => key + 1)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Runs</CardTitle>
        <CardDescription>
          {canRun
            ? 'Trigger a new run and watch its status update live.'
            : 'Recent runs (read-only — a run-rank token can trigger new ones).'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {canRun && <NewRunForm onCreated={handleCreated} />}

        {state.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            Loading…
          </div>
        )}

        {state.status === 'error' && (
          <>
            {state.error instanceof ApiForbiddenError ? (
              <div
                data-testid="runs-forbidden"
                className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
              >
                This view requires a <span className="font-medium text-foreground">run-rank</span> (or
                higher) token. Your current token can still use the other Mirador tabs — only this list
                needs a higher role.
              </div>
            ) : (
              <div
                role="alert"
                className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
              >
                {describeApiError(state.error)}
              </div>
            )}
          </>
        )}

        {state.status === 'success' && state.data.length === 0 && (
          <p className="text-sm text-muted-foreground">No runs yet.</p>
        )}

        {state.status === 'success' && state.data.length > 0 && (
          <Table className="block sm:table">
            <TableHeader className="hidden sm:table-header-group">
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Finished</TableHead>
                {canRun && <TableHead>Actions</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody className="block sm:table-row-group">
              {state.data.map((run: RunSummary) => (
                <TableRow
                  key={run.id}
                  className="mb-3 block rounded-lg border border-border p-3 sm:mb-0 sm:table-row sm:rounded-none sm:border-x-0 sm:border-t-0 sm:p-0"
                >
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Run:</span>#{run.id}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Project:</span>
                    {run.project}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Task:</span>
                    {run.task}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Status:</span>
                    <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Started:</span>
                    {formatTimestamp(run.started_at)}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Finished:</span>
                    {formatTimestamp(run.finished_at)}
                  </TableCell>
                  {canRun && (
                    <TableCell className="block pt-2 sm:table-cell sm:pt-2">
                      {run.status === 'running' && (
                        <StopButton run={run} onStopped={handleStopped} />
                      )}
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
