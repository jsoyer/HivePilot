import { Dialog } from '@base-ui/react/dialog'
import { Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Input } from '@/components/ui/input'
import { useLanguage, useT } from '@/lib/i18n'
import { useTheme } from '@/lib/use-theme'
import { cn } from '@/lib/utils'
import type { NavGroup } from './nav/nav-config'

export interface CommandPaletteProps {
  /** Controlled open state — owned by the caller (`Mirador.tsx`) so both the
   * header search button AND this component's own global `Cmd+K`/`Ctrl+K`
   * listener can open it. */
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Reuses `nav-config`'s already-grouped, already-`t()`-resolved nav items
   * (the exact same data `SidebarNav` renders) so the palette's navigation
   * commands can never drift out of sync with the sidebar. */
  navGroups: NavGroup[]
  /** Called with a nav item's `value` when its command runs — the caller
   * wires this to the same view-state setter the sidebar's `Tabs` uses. */
  onNavigate: (value: string) => void
}

interface PaletteCommand {
  id: string
  group: string
  label: string
  run: () => void
}

/** Case- AND accent-insensitive substring match (`useTheme` -> "utiliser"
 * shouldn't miss "Système" style queries typed without their accents). */
function normalize(value: string): string {
  return value
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
}

/**
 * ⌘K / Ctrl+K command palette (Mirador -> "Vigie" upgrade, P1b) — a
 * filterable modal that jumps to any nav view or runs a quick action
 * (toggle theme, switch language).
 *
 * Built on Base UI's `Dialog` primitive (already a dependency — see
 * `components/ui/tabs.tsx`'s use of `@base-ui/react/tabs`) instead of a
 * dedicated command-palette library (`cmdk`, etc.): Base UI's Dialog already
 * provides everything a v1 palette needs for free — focus trap (`modal:
 * true` is the default), Escape-to-close, outside-press-to-close, and focus
 * restored to the previously-focused element on close. The only bespoke
 * code here is the input/filter/keyboard-navigation list itself.
 *
 * Theme/language actions reuse the SAME hooks `ThemeToggle`/
 * `LanguageToggle` already call (`useTheme`/`useLanguage`) — no duplicated
 * state, no prop drilling for those two.
 */
export function CommandPalette({ open, onOpenChange, navGroups, onNavigate }: CommandPaletteProps) {
  const t = useT()
  const { toggle: toggleTheme } = useTheme()
  const { toggle: toggleLanguage } = useLanguage()
  const [query, setQuery] = useState('')
  const [highlighted, setHighlighted] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const commands = useMemo<PaletteCommand[]>(() => {
    const navCommands: PaletteCommand[] = navGroups.flatMap((group) =>
      group.items.map((item) => ({
        id: `nav-${item.value}`,
        group: group.label,
        label: item.label,
        run: () => {
          onNavigate(item.value)
          onOpenChange(false)
        },
      })),
    )
    const actionsGroup = t('palette.actionsGroup')
    const actionCommands: PaletteCommand[] = [
      {
        id: 'action-toggle-theme',
        group: actionsGroup,
        label: t('palette.toggleTheme'),
        run: () => {
          toggleTheme()
          onOpenChange(false)
        },
      },
      {
        id: 'action-toggle-language',
        group: actionsGroup,
        label: t('palette.toggleLanguage'),
        run: () => {
          toggleLanguage()
          onOpenChange(false)
        },
      },
    ]
    return [...navCommands, ...actionCommands]
  }, [navGroups, onNavigate, onOpenChange, t, toggleTheme, toggleLanguage])

  const filtered = useMemo(() => {
    const needle = normalize(query.trim())
    if (!needle) return commands
    return commands.filter((command) => normalize(command.label).includes(needle))
  }, [commands, query])

  const groupedFiltered = useMemo(() => {
    const groups = new Map<string, { command: PaletteCommand; index: number }[]>()
    filtered.forEach((command, index) => {
      const bucket = groups.get(command.group) ?? []
      bucket.push({ command, index })
      groups.set(command.group, bucket)
    })
    return Array.from(groups.entries())
  }, [filtered])

  // Reset the highlighted row whenever the filtered set changes shape, or
  // the palette re-opens (never carry a stale highlight across sessions).
  useEffect(() => {
    setHighlighted(0)
  }, [query, open])

  // Clear the query on close so the next open always starts from the full
  // (unfiltered) command list.
  useEffect(() => {
    if (!open) setQuery('')
  }, [open])

  // Global `Cmd+K` / `Ctrl+K` — works from anywhere in the app, not just
  // while the palette's own input is focused. While the palette is already
  // open, this listener steps aside entirely (Base UI's Dialog already owns
  // Escape/outside-press dismissal) rather than trying to re-open or toggle
  // it, per the "don't hijack when already open" requirement.
  useEffect(() => {
    function handleGlobalKeyDown(event: KeyboardEvent) {
      const isModK = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k'
      if (!isModK || open) return
      event.preventDefault()
      onOpenChange(true)
    }
    window.addEventListener('keydown', handleGlobalKeyDown)
    return () => window.removeEventListener('keydown', handleGlobalKeyDown)
  }, [open, onOpenChange])

  useEffect(() => {
    if (!open) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${highlighted}"]`)
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [highlighted, open])

  function handleInputKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (filtered.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setHighlighted((prev) => (prev + 1) % filtered.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setHighlighted((prev) => (prev - 1 + filtered.length) % filtered.length)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      filtered[highlighted]?.run()
    }
  }

  const activeOption = filtered[highlighted]

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/60" />
        <Dialog.Popup
          initialFocus={inputRef}
          aria-label={t('palette.title')}
          className="fixed inset-x-3 top-[10vh] z-50 mx-auto flex max-h-[70vh] w-auto max-w-lg flex-col overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-xl sm:inset-x-0"
        >
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <Input
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder={t('palette.placeholder')}
              aria-label={t('palette.placeholder')}
              role="combobox"
              aria-expanded="true"
              aria-controls="command-palette-list"
              aria-activedescendant={activeOption ? `command-palette-option-${activeOption.id}` : undefined}
              autoComplete="off"
              className="h-8 border-none px-0 shadow-none focus-visible:ring-0"
            />
          </div>
          <div
            ref={listRef}
            id="command-palette-list"
            role="listbox"
            aria-label={t('palette.title')}
            className="overflow-y-auto p-1"
          >
            {filtered.length === 0 && (
              <p className="px-3 py-6 text-center text-sm text-muted-foreground">{t('palette.noResults')}</p>
            )}
            {groupedFiltered.map(([groupLabel, items]) => (
              <div key={groupLabel} className="mb-1 last:mb-0">
                <p className="px-2 py-1 text-[11px] font-semibold tracking-wide text-muted-foreground/70 uppercase">
                  {groupLabel}
                </p>
                {items.map(({ command, index }) => (
                  <button
                    key={command.id}
                    id={`command-palette-option-${command.id}`}
                    type="button"
                    role="option"
                    aria-selected={index === highlighted}
                    data-index={index}
                    onMouseEnter={() => setHighlighted(index)}
                    onClick={() => command.run()}
                    className={cn(
                      'flex w-full items-center rounded-md px-2 py-1.5 text-left text-sm text-foreground/80 hover:bg-muted hover:text-foreground',
                      index === highlighted && 'bg-muted text-foreground',
                    )}
                  >
                    {command.label}
                  </button>
                ))}
              </div>
            ))}
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
