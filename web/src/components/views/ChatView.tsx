import { useCallback, useMemo, useState } from 'react'
import { Bot } from 'lucide-react'
import { ChatLayout, ChatMessage, ChatMessageBubble, ChatMessageList } from '@astryxdesign/core/Chat'
import { TextArea } from '@astryxdesign/core/TextArea'
import { Button } from '@astryxdesign/core/Button'
import { RoleAvatar } from '@/components/RoleAvatar'
import { useT } from '@/lib/i18n'
import { askConcierge, type ConciergeDecision } from '@/lib/pollen-api'

/**
 * Talk to the agents in natural language (HP-22) — rebuilt on Meta's **Astryx**
 * design system as the HP-23 POC (evaluate replacing shadcn). Uses Astryx's
 * purpose-built Chat family (ChatLayout / ChatMessageList / ChatMessage /
 * ChatMessageBubble) + TextArea + Button, instead of hand-rolled shadcn +
 * Tailwind bubbles. The concierge wiring (`askConcierge`) and the HP-20
 * `RoleAvatar` are unchanged.
 *
 * Still CLASSIFY-only: an `answer` renders as a bubble; a route/action/
 * multi_route is shown as a PROPOSAL (execution stays behind Approvals/Runs).
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

function BotAvatar() {
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-flex',
        width: 32,
        height: 32,
        alignItems: 'center',
        justifyContent: 'center',
        borderRadius: '9999px',
        background: 'var(--color-surface-raised, rgba(255,255,255,0.08))',
      }}
    >
      <Bot size={16} />
    </span>
  )
}

function ProposalRows({ decision }: { decision: ConciergeDecision }) {
  const t = useT()
  const rows =
    decision.kind === 'multi_route'
      ? decision.dispatches
      : decision.role_key
        ? [{ role_key: decision.role_key, target: decision.target, order: decision.order ?? '' }]
        : []
  return (
    <div data-testid="chat-proposal">
      <p style={{ fontWeight: 600, marginBottom: 8 }}>{t('chat.proposalTitle')}</p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {rows.map((r, i) => (
          <div key={`${r.role_key}-${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <RoleAvatar role={r.role_key} label={r.role_key} size={22} state="thinking" />
            <span>
              <strong>{r.role_key}</strong>
              {r.target ? ` · ${r.target}` : ''}
              {r.order ? ` — ${r.order}` : ''}
            </span>
          </div>
        ))}
      </div>
      <p style={{ opacity: 0.7, fontSize: 12, marginTop: 8 }}>{t('chat.proposalNote')}</p>
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

  const composer = (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', padding: 8 }}>
      <div style={{ flex: 1 }}>
        <TextArea
          label={t('chat.inputAria')}
          isLabelHidden
          value={text}
          onChange={(v) => setText(v)}
          placeholder={t('chat.placeholder')}
          rows={2}
          isDisabled={pending}
        />
      </div>
      <Button
        label={t('chat.send')}
        variant="primary"
        onClick={() => void send()}
        isDisabled={!text.trim() || pending}
        isLoading={pending}
      />
    </div>
  )

  return (
    <div style={{ height: 'calc(100vh - 10rem)', display: 'flex', flexDirection: 'column' }}>
      <ChatLayout
        composer={composer}
        emptyState={
          <div style={{ textAlign: 'center', opacity: 0.7 }}>
            <Bot size={32} aria-hidden="true" />
            <p style={{ marginTop: 8 }}>{t('chat.empty')}</p>
          </div>
        }
      >
        <ChatMessageList isStreaming={pending}>
          {entries.map((entry) => {
            if (entry.from === 'user') {
              return (
                <ChatMessage key={entry.id} sender="user">
                  <ChatMessageBubble variant="filled">{entry.text}</ChatMessageBubble>
                </ChatMessage>
              )
            }
            if (entry.from === 'error') {
              return (
                <ChatMessage key={entry.id} sender="system">
                  <ChatMessageBubble variant="ghost">
                    <span data-testid="chat-message-error" role="alert">
                      {entry.text}
                    </span>
                  </ChatMessageBubble>
                </ChatMessage>
              )
            }
            const d = entry.decision
            return (
              <ChatMessage key={entry.id} sender="assistant" avatar={<BotAvatar />}>
                {d.answer_text && <ChatMessageBubble variant="ghost">{d.answer_text}</ChatMessageBubble>}
                {d.kind !== 'answer' && (
                  <ChatMessageBubble variant="ghost">
                    <ProposalRows decision={d} />
                  </ChatMessageBubble>
                )}
              </ChatMessage>
            )
          })}
          {pending && (
            <ChatMessage sender="assistant" avatar={<BotAvatar />}>
              <ChatMessageBubble variant="ghost">
                <span data-testid="chat-thinking">{t('chat.thinking')}</span>
              </ChatMessageBubble>
            </ChatMessage>
          )}
        </ChatMessageList>
      </ChatLayout>
    </div>
  )
}
