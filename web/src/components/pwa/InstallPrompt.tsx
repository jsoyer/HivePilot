import { Download } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { useT } from '@/lib/i18n'
import { usePersistedState } from '@/lib/use-persisted-state'

const DISMISS_KEY = 'hivepilot.webui.install-dismissed'

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/**
 * Home-screen install affordance (HP-63). Only renders after the browser
 * fires `beforeinstallprompt` — iOS Safari never does, and a standalone
 * window already installed must stay quiet.
 */
export function InstallPrompt() {
  const t = useT()
  const [dismissed, setDismissed] = usePersistedState(DISMISS_KEY, false)
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null)

  useEffect(() => {
    const standalone =
      window.matchMedia('(display-mode: standalone)').matches ||
      ('standalone' in window.navigator &&
        Boolean((window.navigator as Navigator & { standalone?: boolean }).standalone))
    if (standalone) return

    const onPrompt = (event: Event) => {
      event.preventDefault()
      setDeferred(event as BeforeInstallPromptEvent)
    }
    window.addEventListener('beforeinstallprompt', onPrompt)
    return () => window.removeEventListener('beforeinstallprompt', onPrompt)
  }, [])

  if (dismissed || deferred === null) return null

  return (
    <div
      data-testid="pwa-install"
      className="flex items-center gap-1.5"
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="touch-target gap-1.5"
        onClick={async () => {
          await deferred.prompt()
          const choice = await deferred.userChoice
          if (choice.outcome === 'accepted') setDismissed(true)
          setDeferred(null)
        }}
        aria-label={t('pwa.install')}
        title={t('pwa.installHint')}
      >
        <Download className="size-4" />
        <span className="hidden sm:inline">{t('pwa.install')}</span>
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="touch-target text-muted-foreground"
        onClick={() => setDismissed(true)}
      >
        {t('pwa.dismiss')}
      </Button>
    </div>
  )
}
