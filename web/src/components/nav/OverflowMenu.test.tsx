import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import { TOKEN_CLEARED_EVENT, TOKEN_STORAGE_KEY } from '@/lib/api'

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchPushConfig: vi.fn().mockResolvedValue({ enabled: false, vapid_public_key: null }) }
})

import { OverflowMenu } from './OverflowMenu'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  window.localStorage.clear()
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
})

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('OverflowMenu', () => {
  it('opens a menu with theme, language, and sign-out', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <OverflowMenu />
        </LanguageProvider>,
      )
    })
    expect(container.querySelector('[data-testid="overflow-menu"]')).toBeNull()

    act(() => {
      click(container.querySelector('[data-testid="header-overflow"]') as HTMLElement)
    })

    const menu = container.querySelector('[data-testid="overflow-menu"]')
    expect(menu).not.toBeNull()
    expect(menu?.textContent).toContain('Theme')
    expect(menu?.textContent).toContain('Language')
    expect(menu?.textContent).toContain('Sign out')
    expect(container.querySelector('[aria-label*="theme"]')).not.toBeNull()
    expect(container.querySelector('[aria-label*="French"]')).not.toBeNull()
  })

  it('closes on Escape', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <OverflowMenu />
        </LanguageProvider>,
      )
    })
    act(() => {
      click(container.querySelector('[data-testid="header-overflow"]') as HTMLElement)
    })
    expect(container.querySelector('[data-testid="overflow-menu"]')).not.toBeNull()

    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(container.querySelector('[data-testid="overflow-menu"]')).toBeNull()
  })

  it('sign-out clears the stored token and notifies the gate', () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, 'hp_test')
    const cleared = vi.fn()
    window.addEventListener(TOKEN_CLEARED_EVENT, cleared)

    act(() => {
      root.render(
        <LanguageProvider>
          <OverflowMenu />
        </LanguageProvider>,
      )
    })
    act(() => {
      click(container.querySelector('[data-testid="header-overflow"]') as HTMLElement)
    })
    act(() => {
      click(container.querySelector('[data-testid="sign-out"]') as HTMLElement)
    })

    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull()
    expect(cleared).toHaveBeenCalled()
    window.removeEventListener(TOKEN_CLEARED_EVENT, cleared)
  })
})
