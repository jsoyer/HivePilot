import { Activity, DollarSign, HeartPulse } from 'lucide-react'
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import { THEME_STORAGE_KEY } from '@/lib/use-theme'
import { CommandPalette } from './CommandPalette'
import type { NavGroup } from './nav/nav-config'

const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Overview',
    items: [
      { value: 'analytics', label: 'Analytics', Icon: Activity },
      { value: 'cost', label: 'Cost', Icon: DollarSign },
    ],
  },
  {
    label: 'System',
    items: [{ value: 'health', label: 'Health', Icon: HeartPulse }],
  },
]

let container: HTMLDivElement
let root: Root
let navigated: string[]

function Harness() {
  const [open, setOpen] = useState(false)
  return (
    <LanguageProvider>
      <button type="button" data-testid="opener" onClick={() => setOpen(true)}>
        Opener
      </button>
      <CommandPalette
        open={open}
        onOpenChange={setOpen}
        navGroups={NAV_GROUPS}
        onNavigate={(value) => navigated.push(value)}
      />
    </LanguageProvider>
  )
}

function fireKeyDown(target: EventTarget, init: KeyboardEventInit) {
  target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }))
}

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

function getDialog(): HTMLElement | null {
  return document.body.querySelector('[role="dialog"]')
}

function getInput(): HTMLInputElement | null {
  return document.body.querySelector('input')
}

/** React tracks a controlled input's previous value on the DOM node itself
 * to decide whether to fire `onChange` — setting `.value` directly (without
 * going through the native setter) leaves that tracker out of sync, so
 * React never sees a "real" change and `onChange` doesn't fire. Going
 * through the native `HTMLInputElement.prototype.value` setter (the same
 * trick React Testing Library's `fireEvent` uses internally) keeps the
 * tracker honest. */
function setInputValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  nativeSetter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

/** Base UI's Dialog moves initial/final focus via `requestAnimationFrame`
 * (see `floating-ui-react/utils/enqueueFocus.js`), not synchronously —
 * flush one animation frame so focus assertions see the settled state. */
function flushRaf(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()))
}

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
  navigated = []
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
})

describe('CommandPalette', () => {
  it('is closed by default and opens on Cmd+K / Ctrl+K, closes on Escape', async () => {
    act(() => {
      root.render(<Harness />)
    })
    expect(getDialog()).toBeNull()

    await act(async () => {
      fireKeyDown(document, { key: 'k', metaKey: true })
      await Promise.resolve()
    })
    expect(getDialog()).not.toBeNull()

    await act(async () => {
      fireKeyDown(document, { key: 'Escape' })
      await Promise.resolve()
    })
    expect(getDialog()).toBeNull()

    await act(async () => {
      fireKeyDown(document, { key: 'k', ctrlKey: true })
      await Promise.resolve()
    })
    expect(getDialog()).not.toBeNull()
  })

  it('opens via the opener button (simulating the header search affordance)', async () => {
    act(() => {
      root.render(<Harness />)
    })
    const opener = container.querySelector('[data-testid="opener"]') as HTMLElement

    await act(async () => {
      click(opener)
      await Promise.resolve()
    })
    expect(getDialog()).not.toBeNull()
  })

  it('filters the command list by typed query, and shows an empty state for no matches', async () => {
    act(() => {
      root.render(<Harness />)
    })
    await act(async () => {
      fireKeyDown(window, { key: 'k', metaKey: true })
      await Promise.resolve()
    })

    const dialog = getDialog() as HTMLElement
    expect(dialog.textContent).toContain('Cost')
    expect(dialog.textContent).toContain('Health')

    const input = getInput() as HTMLInputElement
    await act(async () => {
      setInputValue(input, 'cost')
      await Promise.resolve()
    })
    expect(dialog.textContent).toContain('Cost')
    expect(dialog.textContent).not.toContain('Health')

    await act(async () => {
      setInputValue(input, 'zzz-no-match')
      await Promise.resolve()
    })
    expect(dialog.textContent).toContain('No matching commands.')
  })

  it('moves the highlighted item with ArrowDown/ArrowUp (wrapping) and runs it on Enter', async () => {
    act(() => {
      root.render(<Harness />)
    })
    await act(async () => {
      fireKeyDown(window, { key: 'k', metaKey: true })
      await Promise.resolve()
    })
    const input = getInput() as HTMLInputElement

    let options = document.body.querySelectorAll('[role="option"]')
    const total = options.length
    expect(options[0]?.getAttribute('aria-selected')).toBe('true')

    await act(async () => {
      fireKeyDown(input, { key: 'ArrowDown' })
      await Promise.resolve()
    })
    options = document.body.querySelectorAll('[role="option"]')
    expect(options[1]?.getAttribute('aria-selected')).toBe('true')

    // ArrowUp from the top wraps to the LAST item.
    await act(async () => {
      fireKeyDown(input, { key: 'ArrowUp' })
      await Promise.resolve()
      fireKeyDown(input, { key: 'ArrowUp' })
      await Promise.resolve()
    })
    options = document.body.querySelectorAll('[role="option"]')
    expect(options[total - 1]?.getAttribute('aria-selected')).toBe('true')

    // ArrowDown from the last item wraps back to the FIRST item.
    await act(async () => {
      fireKeyDown(input, { key: 'ArrowDown' })
      await Promise.resolve()
    })
    options = document.body.querySelectorAll('[role="option"]')
    expect(options[0]?.getAttribute('aria-selected')).toBe('true')

    await act(async () => {
      fireKeyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    expect(navigated).toEqual(['analytics'])
    expect(getDialog()).toBeNull()
  })

  it('runs the "toggle theme" action and closes the palette', async () => {
    act(() => {
      root.render(<Harness />)
    })
    await act(async () => {
      fireKeyDown(window, { key: 'k', metaKey: true })
      await Promise.resolve()
    })
    const input = getInput() as HTMLInputElement
    await act(async () => {
      setInputValue(input, 'toggle theme')
      await Promise.resolve()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    await act(async () => {
      fireKeyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(getDialog()).toBeNull()
  })

  it('runs the "switch language" action and flips the language live', async () => {
    act(() => {
      root.render(<Harness />)
    })
    await act(async () => {
      fireKeyDown(window, { key: 'k', metaKey: true })
      await Promise.resolve()
    })
    const input = getInput() as HTMLInputElement
    await act(async () => {
      setInputValue(input, 'switch language')
      await Promise.resolve()
    })

    await act(async () => {
      fireKeyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('fr'))
  })

  it('traps focus inside the dialog and returns focus to the opener on close', async () => {
    act(() => {
      root.render(<Harness />)
    })
    const opener = container.querySelector('[data-testid="opener"]') as HTMLElement
    opener.focus()
    expect(document.activeElement).toBe(opener)

    await act(async () => {
      click(opener)
      await flushRaf()
    })
    const input = getInput() as HTMLInputElement
    expect(document.activeElement).toBe(input)

    await act(async () => {
      fireKeyDown(document, { key: 'Escape' })
      await flushRaf()
    })
    expect(getDialog()).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('renders palette labels translated in French when lang=fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    act(() => {
      root.render(<Harness />)
    })
    await act(async () => {
      fireKeyDown(window, { key: 'k', metaKey: true })
      await Promise.resolve()
    })
    const dialog = getDialog() as HTMLElement
    expect(dialog.textContent).toContain('Changer de thème (clair/sombre)')
    expect(dialog.textContent).toContain('Changer de langue (EN/FR)')
    const input = getInput() as HTMLInputElement
    expect(input.getAttribute('placeholder')).toBe('Rechercher des vues et actions…')
  })
})
