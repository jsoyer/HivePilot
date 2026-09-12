import { Bell } from 'lucide-react'
import { useT } from '@/lib/i18n'

/**
 * Alerts door (redesign PR1) — a reachable destination, not the Alerts list.
 * The list / triage surface is a follow-up PR. The header chip already
 * counts failed runs + degraded plugins.
 */
export function AlertsView() {
  const t = useT()
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-4" data-testid="alerts-page">
      <header className="flex flex-col gap-1">
        <h2 className="text-3xl font-semibold tracking-tight">{t('alerts.title')}</h2>
        <p className="text-sm text-muted-foreground">{t('alerts.subtitle')}</p>
      </header>
      <div className="flex flex-col items-start gap-3 rounded-[10px] border border-border bg-card p-6">
        <span className="inline-flex size-9 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <Bell className="size-4" aria-hidden="true" />
        </span>
        <p className="text-sm text-muted-foreground">{t('alerts.placeholder')}</p>
      </div>
    </div>
  )
}
