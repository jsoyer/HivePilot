import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Send } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { RoleAvatar } from '@/components/RoleAvatar'
import { useT } from '@/lib/i18n'
import { askConcierge, type ConciergeDecision } from '@/lib/pollen-api'

/**
 * Talk to the agents in natural language (HP-22) — a Grok-Bot-style chat panel
 * backed by the SAME concierge brain the Telegram bot uses (`POST /v1/concierge`
 * → `concierge_service.route`).
 *
 * This surface CLASSIFIES only: an `answer` renders as a bubble; a
 * `route`/`action`/`multi_route` is shown as a PROPOSAL card (execution stays
 * behind the existing Approvals/Runs flows), so the panel is safe at the read
 * role and can never dispatch work on its own.
 */

let counter = 0
function uid(): string {
  counter += 1
  return `m-${counter}-${Date.now()}`
}

type ChatEntry =
  | { id: string; from: 'user'; text: string }
  | { id: string; from: 'concierge'; decision: ConciergeDecision }
  | { id: string; from: 'error'; text: string }

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end" data-testid="chat-message-user">
      <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-primary px-4 py-2 text-sm text-primary-foreground">
        {text}
      </div>
    </div>
  )
}

function ConciergeBubble({ decision }: { decision: ConciergeDecision }) {
  const t = useT()
  return (
    <div className="flex items-start gap-2" data-testid="chat-message-concierge">
      <span className="mt-0.5 inline-flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Bot className="size-4" aria-hidden="true" />
      </span>
      <div className="flex max-w-[80%] flex-col gap-2">
        {decision.answer_text && (
          <div className="whitespace-pre-wrap break-words rounded-2xl rounded-bl-sm bg-muted px-4 py-2 text-sm">
            {decision.answer_text}
          </div>
        )}
        {decision.kind !== 'answer' && <ProposalCard decision={decision} note={t('chat.proposalNote')} />}
      </div>
    </div>
  )
}

function ProposalCard({ decision, note }: { decision: ConciergeDecision; note: string }) {
  const t = useT()
  const rows =
    decision.kind === 'multi_route'
      ? decision.dispatches
      : decision.role_key
        ? [{ role_key: decision.role_key, target: decision.target, order: decision.order ?? '' }]
        : []

  return (
    <div
      data-testid="chat-proposal"
      className="rounded-2xl rounded-bl-sm border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm"
    >
      <p className="mb-2 font-medium">{t('chat.proposalTitle')}</p>
      {rows.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {rows.map((r, i) => (
            <li key={`${r.role_key}-${i}`} className="flex items-center gap-2">
              <RoleAvatar role={r.role_key} label={r.role_key} size={22} state="thinking" />
              <span>
                <span className="font-medium">{r.role_key}</span>
                {r.target ? ` · ${r.target}` : ''}
                {r.order ? ` — ${r.order}` : ''}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p>
          {decision.action ?? decision.kind}
          {decision.target ? ` · ${decision.target}` : ''}
        </p>
      )}
      <p className="mt-2 text-xs text-muted-foreground">{note}</p>
    </div>
  )
}

export function ChatView() {
  const t = useT()
  const conversationId = useMemo(
    () =>
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `web-${Date.now()}`,
    [],
  )
  const [entries, setEntries] = useState<ChatEntry[]>([])
  const [text, setText] = useState('')
  const [pending, setPending] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [entries, pending])

  const send = useCallback(async () => {
    const trimmed = text.trim()
    if (!trimmed || pending) return
    setEntries((prev) => [...prev, { id: uid(), from: 'user', text: trimmed }])
    setText('')
    setPending(true)
    try {
      const decision = await askConcierge(trimmed, conversationId)
      setEntries((prev) => [...prev, { id: uid(), from: 'concierge', decision }])
    } catch {
      setEntries((prev) => [...prev, { id: uid(), from: 'error', text: t('chat.error') }])
    } finally {
      setPending(false)
    }
  }, [text, pending, conversationId, t])

  return (
    <div className="flex h-[calc(100vh-10rem)] flex-col gap-3">
      <div className="flex-1 overflow-y-auto rounded-lg border border-border bg-background/40 p-4">
        {entries.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-muted-foreground">
            <Bot className="size-8" aria-hidden="true" />
            <p className="text-sm">{t('chat.empty')}</p>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {entries.map((entry) => {
              if (entry.from === 'user') return <UserBubble key={entry.id} text={entry.text} />
              if (entry.from === 'error')
                return (
                  <div
                    key={entry.id}
                    role="alert"
                    data-testid="chat-message-error"
                    className="text-sm text-destructive"
                  >
                    {entry.text}
                  </div>
                )
              return <ConciergeBubble key={entry.id} decision={entry.decision} />
            })}
            {pending && (
              <div
                className="flex items-center gap-2 text-sm text-muted-foreground"
                data-testid="chat-thinking"
              >
                <span className="inline-flex size-8 items-center justify-center rounded-full bg-muted">
                  <Bot className="size-4" aria-hidden="true" />
                </span>
                {t('chat.thinking')}
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="flex items-end gap-2">
        <textarea
          aria-label={t('chat.inputAria')}
          data-testid="chat-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
          rows={2}
          placeholder={t('chat.placeholder')}
          className="min-h-[2.5rem] flex-1 resize-none rounded-md border border-border bg-background p-2 text-sm"
        />
        <Button
          data-testid="chat-send"
          onClick={() => void send()}
          disabled={!text.trim() || pending}
          aria-label={t('chat.send')}
        >
          <Send className="size-4" aria-hidden="true" />
        </Button>
      </div>
    </div>
  )
}
