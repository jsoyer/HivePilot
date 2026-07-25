import { ListChecks, Pause, Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Gauge } from '@/components/dashboard/Gauge'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import type { VizTone } from '@/components/dashboard/Sparkline'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  type AutopilotDispatch,
  type AutopilotQueueItem,
  type AutopilotState,
  fetchAutopilot,
  pauseAutopilot,
  resumeAutopilot,
} from '@/lib/mirador-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'
import { AsyncSection } from './AsyncSection'

/** Modest live-ish poll cadence — this is a control-plane summary (queue/
 * budget/dispatches), not an actively-watched run list, so a slower cadence
 * than `RunBoardView`'s 3s is appropriate; mirrors `HomeView`'s own 5s
 * summary-dashboard cadence. */
const POLL_INTERVAL_MS = 5000

function formatCost(n: number): string {
  return `$${n.toFixed(2)}`
}

/** Elapsed time from `iso` to now, as a short "2d"/"3h"/"12m"/"45s" string —
 * same convention as `RunBoardView`/`HomeView`'s own copy of this helper.
 * Unparseable/missing input renders as "—", never a fabricated age. */
function formatAge(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const totalSeconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  if (days > 0) return `${days}d`
  if (hours > 0) return `${hours}h`
  if (minutes > 0) return `${minutes}m`
  return `${totalSeconds}s`
}

/** A dispatch's `outcome` -> viz tone. `_AUTOPILOT_DISPATCHED_STATES` in
 * `api_service.py` only ever surfaces `"done"`/`"blocked"` through this
 * field today, but `autopilot_queue.py`'s state machine also has a
 * `"vetoed"` terminal state — treated the same as `"blocked"` (a dispatch
 * that didn't happen) rather than assuming it can never appear here. Any
 * other/unrecognized outcome renders neutral, never guessed as good/bad. */
function dispatchTone(outcome: string): VizTone {
  if (outcome === 'done') return 'good'
  if (outcome === 'blocked' || outcome === 'vetoed') return 'crit'
  return 'default'
}

interface ControlButtonProps {
  paused: boolean
  canControl: boolean
  onChanged: () => void
}

/**
 * Pause/Resume control (`POST /v1/autopilot/pause|resume`) — changes
 * autonomous behavior, so it always confirms first. `disabled` whenever the
 * caller ranks below `run` (defense-in-depth; the server enforces the same
 * `run` role regardless of what the client shows, see `post_autopilot_pause`/
 * `post_autopilot_resume` in `api_service.py`) — but a 403 that still makes
 * it through (a token demoted mid-session, a race with `useRole`'s own
 * async `whoami()` resolution) is ALSO handled gracefully here, never a
 * crash, mirroring `RunBoardView`'s `StopButton`.
 */
function ControlButton({ paused, canControl, onChanged }: ControlButtonProps) {
  const t = useT()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleClick() {
    const confirmed = window.confirm(paused ? t('autopilot.resumeConfirm') : t('autopilot.pauseConfirm'))
    if (!confirmed) return
    setSubmitting(true)
    setError(null)
    try {
      if (paused) {
        await resumeAutopilot()
      } else {
        await pauseAutopilot()
      }
      onChanged()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('autopilot.insufficientRole') : describeApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant={paused ? 'default' : 'outline'}
          disabled={!canControl || submitting}
          onClick={() => {
            void handleClick()
          }}
          aria-label={paused ? t('autopilot.resumeButton') : t('autopilot.pauseButton')}
          className="gap-2"
        >
          {paused ? <Play className="size-4" /> : <Pause className="size-4" />}
          {submitting
            ? t('common.processing')
            : paused
              ? t('autopilot.resumeButton')
              : t('autopilot.pauseButton')}
        </Button>
        {!canControl && (
          <span className="text-xs text-muted-foreground">{t('autopilot.controlRequiresRunRole')}</span>
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
 * Budget section — real-or-honest-null, field by field (see the module
 * comment in `mirador-api.ts` above `fetchAutopilot`). `budget_daily_usd
 * === null` means no daily budget is configured at all: an honest note, no
 * gauge (there is nothing to burn against). Otherwise, `budget_spent_today`/
 * `budget_remaining` render "unknown" whenever `null` (a spend-lookup
 * failure) — NEVER `$0.00`/the full budget, which would look like a real
 * measurement. The burn Gauge only renders when spend is actually known —
 * a fraction can't be honestly computed from an unknown numerator.
 */
function BudgetSection({ data }: { data: AutopilotState }) {
  const t = useT()

  if (data.budget_daily_usd == null) {
    return (
      <p data-testid="autopilot-no-budget" className="text-sm text-muted-foreground">
        {t('autopilot.noBudget')}
      </p>
    )
  }

  const dailyBudget = data.budget_daily_usd
  const spentKnown = data.budget_spent_today != null
  const remainingKnown = data.budget_remaining != null
  const fraction = spentKnown ? Math.max(0, Math.min(1, data.budget_spent_today! / dailyBudget)) : 0
  const gaugeTone: VizTone = fraction >= 1 ? 'crit' : fraction >= 0.8 ? 'warn' : 'good'

  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:gap-6">
      <div className="grid flex-1 grid-cols-1 gap-4 sm:grid-cols-3">
        <MetricReadout label={t('autopilot.dailyBudget')} value={formatCost(dailyBudget)} />
        <MetricReadout
          label={t('autopilot.spentToday')}
          value={spentKnown ? formatCost(data.budget_spent_today!) : t('autopilot.unknown')}
          tone={spentKnown ? 'default' : 'warn'}
        />
        <MetricReadout
          label={t('autopilot.remaining')}
          value={remainingKnown ? formatCost(data.budget_remaining!) : t('autopilot.unknown')}
          tone={remainingKnown ? 'default' : 'warn'}
        />
      </div>
      {spentKnown && <Gauge value={fraction} label={t('autopilot.budgetBurn')} tone={gaugeTone} />}
    </div>
  )
}

/** Untrusted free text (`pipeline`/`project`/`reason` are pipeline/CLI
 * caller-supplied, same caveat class as `RunSummary.detail` elsewhere in
 * this app) — rendered via plain JSX interpolation only, never
 * `dangerouslySetInnerHTML`. */
function QueueSection({ queue }: { queue: AutopilotQueueItem[] }) {
  const t = useT()

  if (queue.length === 0) {
    return (
      <p data-testid="autopilot-queue-empty" className="text-sm text-muted-foreground">
        {t('autopilot.queueEmpty')}
      </p>
    )
  }

  return (
    <ul className="flex flex-col gap-2">
      {queue.map((item) => (
        <li
          key={item.id}
          data-testid={`autopilot-queue-item-${item.id}`}
          className="flex flex-col gap-1 rounded-lg border border-border p-3 text-sm"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{item.pipeline}</span>
            <span className="text-muted-foreground">·</span>
            <span>{item.project}</span>
            <Badge variant="outline" className="ml-auto">
              {item.state}
            </Badge>
          </div>
          {item.reason && <p className="text-xs text-muted-foreground">{item.reason}</p>}
          <span className="text-xs text-muted-foreground">
            {t('autopilot.enqueuedAgo', { age: formatAge(item.enqueued_at) })}
          </span>
        </li>
      ))}
    </ul>
  )
}

const DISPATCH_BADGE_VARIANT: Record<VizTone, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  default: 'secondary',
  good: 'default',
  warn: 'secondary',
  crit: 'destructive',
}

/** Untrusted free text (`pipeline`/`project`) rendered as plain JSX text,
 * same caveat as `QueueSection` above. Severity stripe only on a
 * non-nominal outcome (`blocked`/`vetoed`) — a `done` dispatch is nominal
 * and gets no stripe, mirroring `RunBoardView`'s `STRIPE_CLASS` convention. */
function DispatchesSection({ dispatches }: { dispatches: AutopilotDispatch[] }) {
  const t = useT()

  if (dispatches.length === 0) {
    return (
      <p data-testid="autopilot-dispatches-empty" className="text-sm text-muted-foreground">
        {t('autopilot.dispatchesEmpty')}
      </p>
    )
  }

  return (
    <ul className="flex flex-col gap-2">
      {dispatches.map((dispatch, index) => {
        const tone = dispatchTone(dispatch.outcome)
        return (
          <li
            key={`${dispatch.pipeline}-${dispatch.project}-${dispatch.at}-${index}`}
            className={cn(
              'flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm',
              tone === 'crit' && 'border-l-4 border-l-[var(--color-crit)]',
            )}
          >
            <span className="font-medium">{dispatch.pipeline}</span>
            <span className="text-muted-foreground">·</span>
            <span>{dispatch.project}</span>
            <Badge variant={DISPATCH_BADGE_VARIANT[tone]}>{dispatch.outcome}</Badge>
            <span className="ml-auto text-xs text-muted-foreground">{formatAge(dispatch.at)}</span>
          </li>
        )
      })}
    </ul>
  )
}

/** `auto_dispatch_allowlist` is a real, config-sourced list of pipeline
 * names — never user free text, but still rendered as plain JSX text (not
 * that it needs escaping, just consistent with the rest of this view). An
 * empty allowlist is a meaningful, honest state: no pipeline is currently
 * allowed to auto-dispatch, so autopilot can queue objectives but never
 * actually drain them. */
function AllowlistSection({ allowlist }: { allowlist: string[] }) {
  const t = useT()

  if (allowlist.length === 0) {
    return (
      <p data-testid="autopilot-allowlist-empty" className="text-sm text-muted-foreground">
        {t('autopilot.allowlistEmpty')}
      </p>
    )
  }

  return (
    <div className="flex flex-wrap gap-2">
      {allowlist.map((name) => (
        <Badge key={name} variant="outline">
          {name}
        </Badge>
      ))}
    </div>
  )
}

/**
 * Autopilot view — `GET /v1/autopilot` (tenant-locked, real-or-honest-empty
 * state, see `mirador-api.ts`'s module comment above `fetchAutopilot`),
 * polled every `POLL_INTERVAL_MS` so a pause/resume or a fresh dispatch
 * shows up without a manual refresh. Pause/Resume (`POST /v1/autopilot/
 * pause|resume`) is gated at `run` server-side; the control disables for a
 * lower-rank caller and also handles a stray 403 gracefully (see
 * `ControlButton`).
 *
 * Every section renders its own honest state: `budget_daily_usd === null`
 * -> "no budget configured" (no gauge); a `null` spend/remaining -> "unknown"
 * (never `0`/full budget); an empty queue/dispatches/allowlist -> a plain
 * "nothing here" message, never a blank list that looks like a loading
 * glitch. `pipeline`/`project`/`reason` are untrusted free text (see
 * `QueueSection`/`DispatchesSection`) — rendered via JSX interpolation only.
 */
export function AutopilotView() {
  const t = useT()
  const { can } = useRole()
  const canControl = can('run')
  const [refreshKey, setRefreshKey] = useState(0)
  const state = useAsyncData(() => fetchAutopilot(), [refreshKey])
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  useEffect(() => {
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [])

  function handleChanged() {
    setRefreshKey((key) => key + 1)
  }

  return (
    <div className="flex flex-col gap-4">
      <Card
        className={
          state.status === 'success' && state.data.paused
            ? 'border-l-4 border-l-[var(--color-warn)]'
            : undefined
        }
      >
        <CardHeader>
          <CardTitle>{t('nav.autopilot')}</CardTitle>
          <CardDescription>{t('autopilot.description')}</CardDescription>
        </CardHeader>
        <CardContent>
          {isForbidden && (
            <div
              data-testid="autopilot-forbidden"
              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              {t('autopilot.forbidden')}
            </div>
          )}

          {!isForbidden && (
            <AsyncSection state={state} isEmpty={() => false}>
              {(data) => (
                <div className="flex flex-col gap-4">
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <MetricReadout
                      icon={data.paused ? <Pause className="size-4" /> : <Play className="size-4" />}
                      label={t('autopilot.statusLabel')}
                      value={data.paused ? t('autopilot.paused') : t('autopilot.active')}
                      tone={data.paused ? 'warn' : 'good'}
                    />
                    <MetricReadout
                      icon={<ListChecks className="size-4" />}
                      label={t('autopilot.queueDepthLabel')}
                      value={data.queue_depth}
                    />
                  </div>
                  <ControlButton paused={data.paused} canControl={canControl} onChanged={handleChanged} />
                </div>
              )}
            </AsyncSection>
          )}
        </CardContent>
      </Card>

      {!isForbidden && state.status === 'success' && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>{t('autopilot.budgetTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <BudgetSection data={state.data} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('autopilot.queueTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <QueueSection queue={state.data.queue} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('autopilot.dispatchesTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <DispatchesSection dispatches={state.data.recent_dispatches} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('autopilot.allowlistTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <AllowlistSection allowlist={state.data.auto_dispatch_allowlist} />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
