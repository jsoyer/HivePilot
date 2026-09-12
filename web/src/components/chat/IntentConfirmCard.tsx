import { useState } from 'react'
import { Bot } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ApiForbiddenError } from '@/lib/api'
import {
  describeIntentBody,
  dispatchConciergePlan,
  intentKind,
  planConciergeDispatch,
} from '@/lib/concierge-intent'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import type { ConciergeDecision } from '@/lib/pollen-api'

type CardState = 'pending' | 'busy' | 'confirmed' | 'cancelled'

/**
 * ACTION / ROUTE confirm card (redesign PR4).
 *
 * Fail-closed: nothing runs until Confirmer. Annuler and ANSWER never
 * dispatch. Approve/deny and action=run use the existing Approvals / Runs
 * APIs; pipeline and route stay acknowledged-only until a dedicated
 * confirm endpoint exists.
 */
export function IntentConfirmCard({ decision }: { decision: ConciergeDecision }) {
  const t = useT()
  const [state, setState] = useState<CardState>('pending')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const kind = intentKind(decision)
  const body = describeIntentBody(decision, t)
  const plan = planConciergeDispatch(decision)

  async function confirm() {
    setState('busy')
    setError(null)
    try {
      const id = await dispatchConciergePlan(plan, t('chat.intentDenyReason'))
      if (plan.type === 'approval') {
        setDone(
          t('chat.intentDoneApproval', {
            id: id ?? '',
            result: plan.approve ? t('chat.intentApproved') : t('chat.intentDenied'),
          }),
        )
      } else if (plan.type === 'run') {
        setDone(t('chat.intentDoneRun', { id: id ?? '' }))
      } else {
        setDone(t('chat.intentDoneAck'))
      }
      setState('confirmed')
    } catch (err) {
      setError(
        err instanceof ApiForbiddenError
          ? t(plan.type === 'run' ? 'runs.insufficientRoleCreate' : 'approvals.insufficientRoleApprove')
          : describeApiError(err),
      )
      setState('pending')
    }
  }

  function cancel() {
    setError(null)
    setDone(t('chat.intentCancelled'))
    setState('cancelled')
  }

  return (
    <div
      data-testid="inbox-intent-card"
      data-intent={kind}
      data-state={state}
      className="w-full min-w-[16rem] rounded-[12px] border border-border bg-card px-4 py-3 text-sm"
    >
      <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
        <span className="inline-flex size-6 items-center justify-center rounded-full bg-muted">
          <Bot className="size-3.5" aria-hidden="true" />
        </span>
        {t('nav.chat')}
      </div>
      <p className="font-medium text-foreground">{t('chat.intentTitle')}</p>
      <p className="mt-1 whitespace-pre-wrap text-foreground">{body}</p>
      <p className="mt-3 border-t border-border pt-2 text-[11px] tracking-wide text-muted-foreground uppercase">
        {t('chat.intentMeta', { kind })}
      </p>
      {state === 'confirmed' || state === 'cancelled' ? (
        <p className="mt-3 text-sm text-muted-foreground" data-testid="inbox-intent-done">
          {done}
        </p>
      ) : (
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button
            data-testid="inbox-intent-confirm"
            disabled={state === 'busy'}
            className="rounded-full bg-[var(--color-good)]/15 text-[var(--color-good)] hover:bg-[var(--color-good)]/25"
            onClick={() => void confirm()}
          >
            {`✅ ${t('chat.intentConfirm')}`}
          </Button>
          <Button
            data-testid="inbox-intent-cancel"
            disabled={state === 'busy'}
            variant="ghost"
            className="rounded-full bg-[var(--color-crit)]/10 text-[var(--color-crit)] hover:bg-[var(--color-crit)]/20"
            onClick={cancel}
          >
            {`❌ ${t('chat.intentCancel')}`}
          </Button>
        </div>
      )}
      {error ? (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  )
}
