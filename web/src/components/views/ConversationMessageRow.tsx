import { Badge } from '@/components/ui/badge'
import { useT } from '@/lib/i18n'
import type { ConversationMessage } from '@/lib/pollen-api'

function firstLine(body: string): string {
  const line = body.split('\n').find((part) => part.trim()) ?? ''
  return line.length > 80 ? `${line.slice(0, 80)}…` : line
}

export function ConversationMessageRow({
  message,
  defaultOpen = false,
}: {
  message: ConversationMessage
  defaultOpen?: boolean
}) {
  const t = useT()
  const preview = firstLine(message.body)
  const arrowLabel = message.target
    ? t('conversations.handedTo', { actor: message.actor, target: message.target })
    : message.actor

  return (
    <details
      className="flex flex-col gap-1 rounded-md border border-border/60 p-2"
      data-testid={`message-${message.interaction_id}`}
      defaultOpen={defaultOpen}
    >
      <summary
        className="flex cursor-pointer list-none flex-col gap-1 [&::-webkit-details-marker]:hidden"
        data-testid={`message-summary-${message.interaction_id}`}
      >
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-semibold">{message.actor}</span>
          {message.target && (
            <>
              <span aria-hidden="true" className="text-muted-foreground">
                →
              </span>
              <span className="text-sm" aria-label={arrowLabel}>
                {message.target}
              </span>
            </>
          )}
          {message.role && (
            <Badge variant="outline" className="text-xs">
              {message.role}
            </Badge>
          )}
          {message.at && (
            <span className="text-xs tabular-nums text-muted-foreground">{message.at}</span>
          )}
        </div>
        {preview && <span className="text-xs text-muted-foreground">{preview}</span>}
      </summary>
      <pre
        data-testid={`message-output-${message.interaction_id}`}
        className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-3 text-sm"
      >
        {message.body}
      </pre>
    </details>
  )
}

export function ConversationFil({ messages }: { messages: ConversationMessage[] }) {
  const t = useT()
  if (messages.length === 0) {
    return <p className="text-sm text-muted-foreground">{t('conversations.emptyThread')}</p>
  }
  return (
    <div className="flex flex-col gap-3" data-testid="conversation-fil">
      {messages.map((message, index) => (
        <ConversationMessageRow
          key={message.interaction_id}
          message={message}
          defaultOpen={index === 0}
        />
      ))}
    </div>
  )
}
