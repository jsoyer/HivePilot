import { useT } from '@/lib/i18n'
import { ChatView } from './ChatView'

/**
 * Inbox landing (redesign PR1) — the concierge chat, with page chrome.
 * ACTION buttons, the KPI bandeau, and Telegram topics are follow-up PRs.
 */
export function InboxView() {
  const t = useT()
  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] w-full max-w-3xl flex-col gap-6" data-testid="inbox-page">
      <header className="flex flex-col gap-1">
        <h2 className="text-3xl font-semibold tracking-tight">{t('inbox.title')}</h2>
        <p className="text-sm text-muted-foreground">{t('inbox.subtitle')}</p>
      </header>
      <div className="min-h-0 flex-1">
        <ChatView />
      </div>
    </div>
  )
}
