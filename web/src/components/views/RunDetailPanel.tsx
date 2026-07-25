import { X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import { fetchRun, type RunDetail } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'

/** `started_at`/`finished_at`/step `timestamp` are SQL `TIMESTAMP` strings
 * (or `null`/absent) — render in the viewer's locale, falling back to the
 * raw string if it doesn't parse, and an em dash while absent. Mirrors
 * `RunBoardView`'s own `formatTimestamp` (each view owns a small copy of
 * this, same convention as `RunsView`/`ApprovalsView` before it). */
function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function statusVariant(status: string): 'default' | 'secondary' | 'destructive' {
  const normalised = status.trim().toLowerCase()
  if (normalised === 'success' || normalised === 'complete') return 'default'
  return normalised === 'failed' || normalised === 'denied' ? 'destructive' : 'secondary'
}

export interface RunDetailPanelProps {
  /** The run to show detail for, or `null` when the panel is closed — this
   * component renders nothing at all when `null` (no backdrop, no drawer),
   * so `RunBoardView` can mount it unconditionally and just flip `runId`. */
  runId: number | null
  onClose: () => void
}

/**
 * Run detail drill-down — a right-side drawer over `GET /v1/runs/{run_id}`
 * (Mirador Operate section PRD). Opened by clicking a `RunBoardView` card.
 *
 * `GET /v1/runs/{run_id}` requires a `run`-rank token, same gate as
 * `GET /v1/runs` (the board itself, which a caller must already have `run`
 * to even see) — a 403 here is therefore only the mid-session
 * role-downgrade edge case (same carve-out pattern as `RunsView`/
 * `ApprovalsView`'s own 403 handling), rendered as a graceful message
 * instead of `AsyncSection`'s generic error case.
 *
 * `detail`/step `detail` ARE rendered here (unlike `RunSummary.detail` in
 * the board list, which is never rendered) — `get_run` on the backend
 * already redacts every registered secret value before persisting, so this
 * is the one place in the app where that field is trusted enough to show.
 * Still rendered ONLY as plain JSX text (React's auto-escaping) — never
 * `dangerouslySetInnerHTML` — so even a redacted-but-adversarial string
 * (e.g. HTML/script markup a pipeline step echoed back) can never execute
 * as markup, only display as literal text.
 *
 * Honest, never fabricated: an empty `steps` array (the backend genuinely
 * has no per-step detail for this run) renders `runDetail.noSteps`, never a
 * guessed/placeholder timeline.
 */
export function RunDetailPanel({ runId, onClose }: RunDetailPanelProps) {
  const t = useT()
  const state = useAsyncData<RunDetail | null>(
    () => (runId === null ? Promise.resolve(null) : fetchRun(runId)),
    [runId],
  )

  if (runId === null) return null

  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  return (
    <>
      <div
        data-testid="run-detail-backdrop"
        aria-hidden="true"
        className="fixed inset-0 z-40 bg-black/50"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-label={t('runDetail.title', { id: runId })}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col gap-4 overflow-y-auto border-l border-border bg-card p-4 shadow-xl sm:p-6"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">{t('runDetail.title', { id: runId })}</h2>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={t('runDetail.closeAriaLabel')}
            onClick={onClose}
          >
            <X className="size-4" />
          </Button>
        </div>

        {state.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            {t('common.loading')}
          </div>
        )}

        {state.status === 'error' && isForbidden && (
          <div
            data-testid="run-detail-forbidden"
            className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
          >
            {t('runDetail.requiresTokenLead')} <span className="font-medium text-foreground">run-rank</span>{' '}
            {t('runDetail.requiresTokenTail')}
          </div>
        )}

        {state.status === 'error' && !isForbidden && (
          <div
            role="alert"
            className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
          >
            {describeApiError(state.error)}
          </div>
        )}

        {state.status === 'success' && state.data && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant={statusVariant(state.data.status)}>{state.data.status}</Badge>
              <span className="font-medium">{state.data.project}</span>
              <span className="text-muted-foreground">·</span>
              <span>{state.data.task}</span>
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted-foreground">{t('runs.started')}</dt>
              <dd>{formatTimestamp(state.data.started_at)}</dd>
              <dt className="text-muted-foreground">{t('runs.finished')}</dt>
              <dd>{formatTimestamp(state.data.finished_at)}</dd>
              {state.data.detail && (
                <>
                  <dt className="text-muted-foreground">{t('runDetail.overallDetail')}</dt>
                  <dd className="break-words">{state.data.detail}</dd>
                </>
              )}
            </dl>

            <div>
              <h3 className="mb-2 text-sm font-semibold">{t('runDetail.stepsTitle')}</h3>
              {state.data.steps.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t('runDetail.noSteps')}</p>
              ) : (
                <Table className="block sm:table">
                  <TableHeader className="hidden sm:table-header-group">
                    <TableRow>
                      <TableHead>{t('analytics.step')}</TableHead>
                      <TableHead>{t('common.status')}</TableHead>
                      <TableHead>{t('runDetail.provider')}</TableHead>
                      <TableHead>{t('runDetail.model')}</TableHead>
                      <TableHead>{t('runDetail.tokens')}</TableHead>
                      <TableHead>{t('runDetail.cost')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody className="block sm:table-row-group">
                    {state.data.steps.map((step, index) => (
                      <TableRow
                        // eslint-disable-next-line react/no-array-index-key -- step rows have no stable id of their own
                        key={`${step.step}-${index}`}
                        className="mb-3 block rounded-lg border border-border p-3 sm:mb-0 sm:table-row sm:rounded-none sm:border-x-0 sm:border-t-0 sm:p-0"
                      >
                        <TableCell className="block sm:table-cell">
                          <span className="mr-1 font-medium sm:hidden">{t('analytics.step')}:</span>
                          {step.step}
                        </TableCell>
                        <TableCell className="block sm:table-cell">
                          <span className="mr-1 font-medium sm:hidden">{t('common.status')}:</span>
                          <Badge variant={statusVariant(step.status)}>{step.status}</Badge>
                        </TableCell>
                        <TableCell className="block sm:table-cell">
                          <span className="mr-1 font-medium sm:hidden">{t('runDetail.provider')}:</span>
                          {step.provider ?? '—'}
                        </TableCell>
                        <TableCell className="block sm:table-cell">
                          <span className="mr-1 font-medium sm:hidden">{t('runDetail.model')}:</span>
                          {step.model ?? '—'}
                        </TableCell>
                        <TableCell className="block sm:table-cell">
                          <span className="mr-1 font-medium sm:hidden">{t('runDetail.tokens')}:</span>
                          {step.input_tokens ?? '—'}/{step.output_tokens ?? '—'}
                        </TableCell>
                        <TableCell className="block sm:table-cell">
                          <span className="mr-1 font-medium sm:hidden">{t('runDetail.cost')}:</span>
                          {step.cost_usd != null ? `$${step.cost_usd.toFixed(4)}` : '—'}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  )
}
