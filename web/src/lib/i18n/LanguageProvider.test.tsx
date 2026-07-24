import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider, useLanguage, useT } from './LanguageProvider'

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

function Probe() {
  const t = useT()
  const { language, toggle } = useLanguage()
  return (
    <div>
      <span data-testid="lang">{language}</span>
      <span data-testid="overview">{t('nav.overview')}</span>
      <span data-testid="params">{t('common.lastDays', { days: 30 })}</span>
      <span data-testid="missing">{t('nope.does-not-exist')}</span>
      <button data-testid="toggle" onClick={toggle}>
        toggle
      </button>
    </div>
  )
}

function readText(testId: string): string {
  return (container.querySelector(`[data-testid="${testId}"]`) as HTMLElement).textContent ?? ''
}

describe('useLanguage / useT', () => {
  it('defaults to English (and never crashes) when used outside a LanguageProvider', () => {
    act(() => {
      root.render(<Probe />)
    })
    expect(readText('lang')).toBe('en')
    expect(readText('overview')).toBe('Overview')
  })

  it('defaults to English when nothing is persisted', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    expect(readText('lang')).toBe('en')
    expect(readText('overview')).toBe('Overview')
  })

  it('t() returns the French string when the language is fr', () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    expect(readText('lang')).toBe('fr')
    expect(readText('overview')).toBe("Vue d'ensemble")
  })

  it('interpolates {params} into the translated template', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    expect(readText('params')).toBe('Last 30 days')
  })

  it('falls back to the key itself for a missing translation', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    expect(readText('missing')).toBe('nope.does-not-exist')
  })

  it('toggle flips en <-> fr and re-renders translated text live (no reload)', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    expect(readText('overview')).toBe('Overview')

    act(() => {
      click(container.querySelector('[data-testid="toggle"]') as HTMLElement)
    })
    expect(readText('lang')).toBe('fr')
    expect(readText('overview')).toBe("Vue d'ensemble")

    act(() => {
      click(container.querySelector('[data-testid="toggle"]') as HTMLElement)
    })
    expect(readText('lang')).toBe('en')
    expect(readText('overview')).toBe('Overview')
  })

  it('persists the toggled language to localStorage under LANG_STORAGE_KEY', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    act(() => {
      click(container.querySelector('[data-testid="toggle"]') as HTMLElement)
    })
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('fr'))
  })

  it('a fresh mount reads the persisted language back (survives reload)', () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    act(() => {
      root.render(
        <LanguageProvider>
          <Probe />
        </LanguageProvider>,
      )
    })
    expect(readText('lang')).toBe('fr')
  })
})
