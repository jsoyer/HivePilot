import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Send } from 'lucide-react'
import { IntentConfirmCard } from '@/components/chat/IntentConfirmCard'
import { Button } from '@/components/ui/button'
import { CallToggle, useVoiceCall } from '@/components/voice/CallToggle'
import { ComposerMic } from '@/components/voice/ComposerMic'
import { requiresConfirmation } from '@/lib/concierge-intent'
import { useLanguage, useT } from '@/lib/i18n'
import { askConcierge, type ConciergeDecision } from '@/lib/pollen-api'
import { speakReply } from '@/lib/voice-reply'

/**
 * Talk to the agents in natural language (HP-22) — a Grok-Bot-style chat panel
 * backed by the SAME concierge brain the Telegram bot uses (`POST /v1/concierge`
 * → `concierge_service.route`).
 *
 * This surface CLASSIFIES only until the operator confirms. An `answer`
 * renders as a bubble with no keyboard. ACTION / ROUTE / MULTI_ROUTE show
 * the confirm card (✅ / ❌). Nothing runs without that card.
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
        {requiresConfirmation(decision) ? <IntentConfirmCard decision={decision} /> : null}
      </div>
    </div>
  )
}

export function ChatView() {
  const t = useT()
  const { language } = useLanguage()
  const inCall = useVoiceCall()
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
      if (inCall && decision.answer_text) {
        void speakReply(decision.answer_text, language === 'fr' ? 'fr-FR' : 'en-US')
      }
    } catch {
      setEntries((prev) => [...prev, { id: uid(), from: 'error', text: t('chat.error') }])
    } finally {
      setPending(false)
    }
  }, [text, pending, conversationId, t, inCall, language])

  return (
    <div className="flex h-full min-h-[24rem] flex-col gap-3">
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
        <ComposerMic
          disabled={pending}
          onTranscript={(chunk) => setText((prev) => (prev ? `${prev.trim()} ${chunk}` : chunk))}
        />
        <CallToggle />
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
