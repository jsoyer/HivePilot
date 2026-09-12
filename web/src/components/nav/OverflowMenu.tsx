import { MoreHorizontal } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { InstallPrompt } from '@/components/pwa/InstallPrompt'
import { PushToggle } from '@/components/pwa/PushToggle'
import { Button } from '@/components/ui/button'
import { clearToken } from '@/lib/api'
import { useT } from '@/lib/i18n'
import { useEscapeKey } from '@/lib/use-escape-key'
import { LanguageToggle } from './LanguageToggle'
import { ThemeToggle } from './ThemeToggle'

/**
 * Header overflow (theme / language / account). Redesign PR1 collapses the
 * old header control strip so only search, the issues chip, and this menu
 * stay visible.
 */
export function OverflowMenu() {
  const t = useT()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEscapeKey(open, () => setOpen(false))

  useEffect(() => {
    if (!open) return
    function handlePointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    window.addEventListener('mousedown', handlePointerDown)
    return () => window.removeEventListener('mousedown', handlePointerDown)
  }, [open])

  return (
    <div ref={rootRef} className="relative" data-slot="overflow-menu">
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="touch-target"
        data-testid="header-overflow"
        aria-label={t('header.overflow')}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((prev) => !prev)}
      >
        <MoreHorizontal className="size-4" />
      </Button>
      {open && (
        <div
          role="menu"
          data-testid="overflow-menu"
          className="absolute top-full right-0 z-50 mt-2 flex w-56 flex-col gap-1 rounded-[10px] border border-border bg-card p-2 shadow-lg"
        >
          <div className="flex items-center justify-between gap-2 rounded-[10px] px-2 py-1">
            <span className="text-sm text-muted-foreground">{t('header.theme')}</span>
            <ThemeToggle />
          </div>
          <div className="flex items-center justify-between gap-2 rounded-[10px] px-2 py-1">
            <span className="text-sm text-muted-foreground">{t('header.language')}</span>
            <LanguageToggle />
          </div>
          <InstallPrompt />
          <PushToggle />
          <div className="my-1 h-px bg-border" />
          <button
            type="button"
            role="menuitem"
            data-testid="sign-out"
            className="rounded-[10px] px-2 py-2 text-left text-sm hover:bg-muted"
            onClick={() => {
              setOpen(false)
              clearToken()
            }}
          >
            {t('header.signOut')}
          </button>
        </div>
      )}
    </div>
  )
}
