import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { type Approval, fetchApprovals, postApproval } from '@/lib/mirador-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'

/** `requested_at` is a SQL `TIMESTAMP` string — render it in the viewer's
 * locale, falling back to the raw string if it doesn't parse. */
function formatRequestedAt(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

interface RowActionsProps {
  approval: Approval
  onDone: () => void
}

/**
 * Approve/Deny controls for a single pending approval. Owns its own
 * in-flight/error/deny-reason state so one row's action can never affect
 * another's.
 *
 * `POST /v1/approvals/{run_id}` is SYNCHRONOUS — it re-runs the pipeline
 * inline before responding (see `api_service.py`'s `handle_approval`), so
 * this call can block for the full pipeline duration. Both buttons (and the
 * reason input) are disabled and show "Processing…" for that whole window
 * instead of looking frozen or letting a double-click fire a second call.
 *
 * Only rendered by the parent when `useRole().can('approve')` — this
 * component assumes the caller already has at least an `approve` token; a
 * 403 here (token demoted mid-session, cross-tenant edge case, etc.) is
 * still handled gracefully rather than crashing the row.
 */
function RowActions({ approval, onDone }: RowActionsProps) {
  const [denyOpen, setDenyOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(approve: boolean) {
    setSubmitting(true)
    setError(null)
    try {
      await postApproval(approval.run_id, { approve, reason: approve ? undefined : reason.trim() })
      onDone()
    } catch (err) {
      setError(
        err instanceof ApiForbiddenError
          ? 'Insufficient role — your token can no longer approve/deny this run.'
          : describeApiError(err),
      )
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={submitting}
          onClick={() => {
            void submit(true)
          }}
          aria-label={`Approve run ${approval.run_id}`}
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="destructive"
          disabled={submitting}
          onClick={() => setDenyOpen((open) => !open)}
          aria-label={`Deny run ${approval.run_id}`}
        >
          Deny
        </Button>
        {submitting && (
          <span role="status" className="text-sm text-muted-foreground">
            Processing…
          </span>
        )}
      </div>

      {denyOpen && (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Reason for denial (required)…"
            aria-label={`Denial reason for run ${approval.run_id}`}
            disabled={submitting}
          />
          <Button
            size="sm"
            variant="destructive"
            disabled={submitting || !reason.trim()}
            onClick={() => {
              void submit(false)
            }}
          >
            Confirm deny
          </Button>
        </div>
      )}

      {error && (
        <div role="alert" className="text-sm text-destructive">
          {error}
        </div>
      )}
    </div>
  )
}

/**
 * Approvals tab — `GET /v1/approvals` (pending, tenant-filtered), with
 * per-row Approve/Deny via `POST /v1/approvals/{run_id}`.
 *
 * Visible to any token (like every other built-in tab, see `Mirador.tsx`),
 * but:
 *  - `GET /v1/approvals` itself requires a `run`-rank token (stricter than
 *    the token gate's own `read` floor) — a `read` token 403s and sees a
 *    graceful "requires a run token" message, same pattern as
 *    `Mem0View`/`PanelView`.
 *  - Approve/Deny buttons only render for `useRole().can('approve')` — a
 *    `run`-rank (or lower, once the list loads) token sees the list
 *    read-only, no action controls.
 *
 * Never renders `Approval.metadata` (untrusted free text set by whatever
 * pipeline stage requested the approval) — only the typed, structural
 * fields (run id / project / task / requested-at / status).
 */
export function ApprovalsView() {
  const { can } = useRole()
  const canApprove = can('approve')
  const [refreshKey, setRefreshKey] = useState(0)
  const state = useAsyncData(() => fetchApprovals(), [refreshKey])

  function handleDone() {
    setRefreshKey((key) => key + 1)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Approvals</CardTitle>
        <CardDescription>
          {canApprove
            ? 'Pending pipeline approvals — approve or deny below.'
            : 'Pending pipeline approvals (read-only — an approve-rank token can act on these).'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {state.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            Loading…
          </div>
        )}

        {state.status === 'error' && (
          <>
            {state.error instanceof ApiForbiddenError ? (
              <div
                data-testid="approvals-forbidden"
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
          <p className="text-sm text-muted-foreground">No pending approvals.</p>
        )}

        {state.status === 'success' && state.data.length > 0 && (
          <Table className="block sm:table">
            <TableHeader className="hidden sm:table-header-group">
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Requested</TableHead>
                <TableHead>Status</TableHead>
                {canApprove && <TableHead>Actions</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody className="block sm:table-row-group">
              {state.data.map((approval) => (
                <TableRow
                  key={approval.run_id}
                  className="mb-3 block rounded-lg border border-border p-3 sm:mb-0 sm:table-row sm:rounded-none sm:border-x-0 sm:border-t-0 sm:p-0"
                >
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Run:</span>#{approval.run_id}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Project:</span>
                    {approval.project}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Task:</span>
                    {approval.task}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Requested:</span>
                    {formatRequestedAt(approval.requested_at)}
                  </TableCell>
                  <TableCell className="block sm:table-cell">
                    <span className="mr-1 font-medium sm:hidden">Status:</span>
                    <Badge variant="secondary">{approval.status}</Badge>
                  </TableCell>
                  {canApprove && (
                    <TableCell className="block pt-2 sm:table-cell sm:pt-2">
                      <RowActions approval={approval} onDone={handleDone} />
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
