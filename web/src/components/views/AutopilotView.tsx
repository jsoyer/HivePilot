import { ListChecks, Pause, Play, Send, ShieldCheck, Wallet } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { Gauge } from '@/components/dashboard/Gauge'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { SectionHeader } from '@/components/dashboard/SectionHeader'
import type { VizTone } from '@/components/dashboard/Sparkline'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { EM_DASH, formatAge } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  type AutopilotDispatch,
  type AutopilotQueueItem,
  type AutopilotState,
  fetchAutopilot,
  pauseAutopilot,
  resumeAutopilot,
} from '@/lib/pollen-api'
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
    <div className="flex flex-col items-end gap-1">
      <Button
        size="sm"
        variant={paused ? 'default' : 'outline'}
        disabled={!canControl || submitting}
        onClick={() => {
          void handleClick()
        }}
        aria-label={paused ? t('autopilot.resumeButton') : t('autopilot.pauseButton')}
        className="gap-2"
      >
        {paused ? <Play className="size-4" /> : <Pause className="size-4" />}
        {submitting ? t('common.processing') : paused ? t('autopilot.resumeButton') : t('autopilot.pauseButton')}
      </Button>
      {!canControl && (
        <span className="text-xs text-muted-foreground">{t('autopilot.controlRequiresRunRole')}</span>
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
 * The whole control-plane state on ONE line of readouts: is it running, how
 * much is waiting, what may it spend, what has it spent.
 *
 * These four were previously spread over two cards and a mostly-blank page.
 * They are the four numbers an operator opens this view for, so they lead.
 *
 * Real-or-honest-null, field by field (see the module comment in
 * `pollen-api.ts` above `fetchAutopilot`): `budget_daily_usd === null` means
 * no ceiling is configured at all; `budget_spent_today`/`budget_remaining`
 * render "unknown" whenever `null` (a spend-lookup failure) — NEVER `$0.00`
 * or the full budget, which would look like a real measurement. The burn
 * Gauge only renders when spend is actually known: a fraction cannot be
 * honestly computed from an unknown numerator.
 */
function StatusRow({ data }: { data: AutopilotState }) {
  const t = useT()
  const budget = data.budget_daily_usd
  const spentKnown = data.budget_spent_today != null
  const fraction =
    budget != null && budget > 0 && spentKnown
      ? Math.max(0, Math.min(1, data.budget_spent_today! / budget))
      : null
  const gaugeTone: VizTone = fraction === null ? 'default' : fraction >= 1 ? 'crit' : fraction >= 0.8 ? 'warn' : 'good'
  // Only claim "blocked" from a MEASURED remaining of zero. An unknown spend
  // must not be rendered as either free or exhausted.
  const budgetExhausted = budget != null && budget > 0 && data.budget_remaining != null && data.budget_remaining <= 0

  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:gap-6">
      {/* Two distinct absences, kept visually distinct: an em-dash means the
       * value is not CONFIGURED (no daily budget exists — see the empty
       * state below it), whereas "unknown" means it exists but could not be
       * MEASURED (a spend-lookup failure). Neither is ever rendered as
       * `$0.00`, which would read as a real measurement. */}
      <div className="grid flex-1 grid-cols-2 gap-4 lg:grid-cols-5">
        {/* Three states, not two. "Active" alongside a 100%-burn gauge and
         * `Remaining $0.00` claimed the autopilot was working when the budget
         * gate had already closed: it is enabled, and it cannot act. An
         * operator reading "Active · nothing dispatched" has no way to tell
         * whether that is calm or blocked. */}
        <MetricReadout
          icon={
            data.paused ? (
              <Pause className="size-4" />
            ) : budgetExhausted ? (
              <Wallet className="size-4" />
            ) : (
              <Play className="size-4" />
            )
          }
          label={t('autopilot.statusLabel')}
          value={
            data.paused
              ? t('autopilot.paused')
              : budgetExhausted
                ? t('autopilot.blockedByBudget')
                : t('autopilot.active')
          }
          tone={data.paused || budgetExhausted ? 'warn' : 'good'}
        />
        <MetricReadout
          icon={<ListChecks className="size-4" />}
          label={t('autopilot.queueDepthLabel')}
          value={data.queue_depth}
        />
        <MetricReadout
          icon={<Wallet className="size-4" />}
          label={t('autopilot.dailyBudget')}
          value={budget == null ? EM_DASH : formatCost(budget)}
        />
        <MetricReadout
          label={t('autopilot.spentTodayTenant')}
          value={spentKnown ? formatCost(data.budget_spent_today!) : t('autopilot.unknown')}
          tone={spentKnown ? 'default' : 'warn'}
        />
        <MetricReadout
          label={t('autopilot.remainingTenant')}
          value={
            data.budget_remaining != null ? formatCost(data.budget_remaining) : t('autopilot.unknown')
          }
          tone={data.budget_remaining != null ? 'default' : 'warn'}
        />
      </div>
      {fraction !== null && (
        <Gauge value={fraction} label={t('autopilot.budgetBurn')} tone={gaugeTone} />
      )}
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
      <EmptyState
        data-testid="autopilot-queue-empty"
        icon={<ListChecks className="size-4" />}
        title={t('autopilot.queueEmptyTitle')}
        body={t('autopilot.queueEmptyBody')}
      />
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
          <span className="metric-mono text-xs text-muted-foreground">
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
      <EmptyState
        data-testid="autopilot-dispatches-empty"
        icon={<Send className="size-4" />}
        title={t('autopilot.dispatchesEmptyTitle')}
        body={t('autopilot.dispatchesEmptyBody')}
      />
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
            <span className="metric-mono ml-auto text-xs text-muted-foreground">
              {formatAge(dispatch.at)}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

/** `auto_dispatch_allowlist` is a real, config-sourced list of pipeline
 * names — never user free text, but still rendered as plain JSX text (not
 * that it needs escaping, just consistent with the rest of this view). An
 * empty allowlist is a meaningful, honest state with a real consequence:
 * autopilot can queue objectives but will never drain one. The empty state
 * says exactly that, instead of "Nothing allowlisted." */
function AllowlistSection({ allowlist }: { allowlist: string[] }) {
  const t = useT()

  if (allowlist.length === 0) {
    return (
      <EmptyState
        data-testid="autopilot-allowlist-empty"
        icon={<ShieldCheck className="size-4" />}
        title={t('autopilot.allowlistEmptyTitle')}
        body={t('autopilot.allowlistEmptyBody')}
      />
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
 * Autopilot — `GET /v1/autopilot` (tenant-locked, real-or-honest-empty
 * state), polled every `POLL_INTERVAL_MS` so a pause/resume or a fresh
 * dispatch shows up without a manual refresh. Pause/Resume is gated at
 * `run` server-side; the control disables for a lower-rank caller and also
 * handles a stray 403 gracefully.
 *
 * Layout: ONE card. The previous version was five near-empty cards stacked
 * down a page that was 80% blank, four of which said some variant of
 * "nothing". The four control-plane numbers now lead on a single row, the
 * queue and the dispatch log sit side by side, and each empty section says
 * what would fill it and what its being empty MEANS — an empty allowlist is
 * not a blank, it is "autopilot will never dispatch".
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

  const paused = state.status === 'success' && state.data.paused

  return (
    <Card className={paused ? 'border-l-4 border-l-[var(--color-warn)]' : undefined}>
      <CardHeader>
        <CardTitle>{t('nav.autopilot')}</CardTitle>
        <CardDescription>{t('autopilot.description')}</CardDescription>
        {state.status === 'success' && (
          <CardAction>
            <ControlButton paused={state.data.paused} canControl={canControl} onChanged={handleChanged} />
          </CardAction>
        )}
      </CardHeader>
      <CardContent>
        {isForbidden ? (
          <div
            data-testid="autopilot-forbidden"
            className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
          >
            {t('autopilot.forbidden')}
          </div>
        ) : (
          <AsyncSection state={state} isEmpty={() => false}>
            {(data) => (
              <div className="flex flex-col gap-6">
                <StatusRow data={data} />

                {data.budget_daily_usd == null && (
                  <EmptyState
                    data-testid="autopilot-no-budget"
                    icon={<Wallet className="size-4" />}
                    title={t('autopilot.noBudgetTitle')}
                    body={t('autopilot.noBudgetBody')}
                  />
                )}

                <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                  <section className="flex flex-col gap-3">
                    <SectionHeader index="01" title={t('autopilot.queueTitle')} />
                    <QueueSection queue={data.queue} />
                  </section>
                  <section className="flex flex-col gap-3">
                    <SectionHeader index="02" title={t('autopilot.dispatchesTitle')} />
                    <DispatchesSection dispatches={data.recent_dispatches} />
                  </section>
                </div>

                <section className="flex flex-col gap-3">
                  <SectionHeader index="03" title={t('autopilot.allowlistTitle')} />
                  <AllowlistSection allowlist={data.auto_dispatch_allowlist} />
                </section>
              </div>
            )}
          </AsyncSection>
        )}
      </CardContent>
    </Card>
  )
}
