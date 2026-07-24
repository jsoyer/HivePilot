import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import { LanguageToggle } from './LanguageToggle'

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

function mount() {
  act(() => {
    root.render(
      <LanguageProvider>
        <LanguageToggle />
      </LanguageProvider>,
    )
  })
}

describe('LanguageToggle', () => {
  it('renders a single accessible button showing the current language', () => {
    mount()
    const button = container.querySelector('button')
    expect(button).not.toBeNull()
    expect(button?.getAttribute('aria-label')).toBeTruthy()
    expect(button?.textContent).toBe('EN')
  })

  it('flips en <-> fr and persists the choice when clicked', () => {
    mount()
    const button = container.querySelector('button') as HTMLElement

    act(() => {
      click(button)
    })
    expect(button.textContent).toBe('FR')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('fr'))

    act(() => {
      click(button)
    })
    expect(button.textContent).toBe('EN')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('en'))
  })

  it('updates its own aria-label to describe the NEXT action after toggling', () => {
    mount()
    const button = container.querySelector('button') as HTMLElement
    const labelWhenEn = button.getAttribute('aria-label')

    act(() => {
      click(button)
    })
    const labelWhenFr = button.getAttribute('aria-label')
    expect(labelWhenFr).not.toBe(labelWhenEn)
  })

  it('starts from a persisted language choice', () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mount()
    const button = container.querySelector('button') as HTMLElement
    expect(button.textContent).toBe('FR')
  })
})
