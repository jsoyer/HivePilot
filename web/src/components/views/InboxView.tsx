import { InboxSnapshot } from './InboxSnapshot'
import { ChatView } from './ChatView'

export interface InboxViewProps {
  onNavigate: (view: string) => void
}

/**
 * Inbox landing (redesign PR4) — concierge chat plus the Snapshot bandeau.
 * ACTION / ROUTE confirm lives in the chat. Telegram topics are PR5.
 */
export function InboxView({ onNavigate }: InboxViewProps) {
  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] w-full max-w-3xl flex-col gap-6" data-testid="inbox-page">
      <InboxSnapshot onNavigate={onNavigate} />
      <div className="min-h-0 flex-1">
        <ChatView />
      </div>
    </div>
  )
}
