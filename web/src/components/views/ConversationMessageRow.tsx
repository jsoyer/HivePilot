import { useState } from 'react'
import { formatClock } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import type { ConversationMessage } from '@/lib/pollen-api'
import { RoleBadge, displayActorName } from './RoleBadge'

export function inferTargetRole(
  messages: ConversationMessage[],
  index: number,
): string | null {
  const target = messages[index]?.target
  if (!target) return null
  const next = messages[index + 1]
  if (next?.actor === target && next.role) return next.role
  return messages.find((message) => message.actor === target)?.role ?? null
}

export function ConversationMessageRow({
  message,
  targetRole = null,
}: {
  message: ConversationMessage
  targetRole?: string | null
}) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const arrowLabel = message.target
    ? t('conversations.handedTo', { actor: message.actor, target: message.target })
    : message.actor

  return (
    <div
      className="overflow-hidden rounded-[10px] border border-border bg-background"
      data-testid={`message-${message.interaction_id}`}
    >
      <button
        type="button"
        className="flex w-full flex-wrap items-center gap-2 px-3 py-2.5 text-left"
        data-testid={`message-summary-${message.interaction_id}`}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        {message.role ? (
          <RoleBadge role={message.role} label={displayActorName(message.actor)} />
        ) : (
          <span className="text-sm font-semibold">{displayActorName(message.actor)}</span>
        )}
        {message.target && (
          <>
            <span aria-hidden="true" className="text-xs text-muted-foreground">
              →
            </span>
            {targetRole ? (
              <RoleBadge role={targetRole} label={displayActorName(message.target)} />
            ) : (
              <span className="text-sm" aria-label={arrowLabel}>
                {displayActorName(message.target)}
              </span>
            )}
          </>
        )}
        {message.action && (
          <span className="text-[11px] text-muted-foreground">{message.action}</span>
        )}
        {message.at && (
          <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">
            {formatClock(message.at)}
          </span>
        )}
        <span className="text-[11px] text-muted-foreground">
          {open ? t('conversations.collapse') : t('conversations.expand')}
        </span>
      </button>
      {open && (
        <pre
          data-testid={`message-output-${message.interaction_id}`}
          className="px-3 pb-3 text-[13px] leading-relaxed whitespace-pre-wrap break-words text-foreground"
        >
          {message.body}
        </pre>
      )}
    </div>
  )
}

export function ConversationFil({ messages }: { messages: ConversationMessage[] }) {
  const t = useT()
  if (messages.length === 0) {
    return <p className="text-sm text-muted-foreground">{t('conversations.emptyThread')}</p>
  }
  return (
    <div className="flex flex-col gap-2" data-testid="conversation-fil">
      {messages.map((message, index) => (
        <ConversationMessageRow
          key={message.interaction_id}
          message={message}
          targetRole={inferTargetRole(messages, index)}
        />
      ))}
    </div>
  )
}
