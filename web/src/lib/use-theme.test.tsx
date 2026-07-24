import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { THEME_STORAGE_KEY, useTheme } from './use-theme'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
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

function Probe() {
  const { theme, toggle } = useTheme()
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button data-testid="toggle" onClick={toggle}>
        toggle
      </button>
    </div>
  )
}

function readTheme(): string {
  return (container.querySelector('[data-testid="theme"]') as HTMLElement).textContent ?? ''
}

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('useTheme', () => {
  it('defaults to light when nothing is stored and <html> has no .dark class', () => {
    act(() => {
      root.render(<Probe />)
    })
    expect(readTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('reads an existing .dark class on <html> (index.html default) as the initial theme', () => {
    document.documentElement.classList.add('dark')
    act(() => {
      root.render(<Probe />)
    })
    expect(readTheme()).toBe('dark')
  })

  it('starts from a persisted theme over the DOM class', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light')
    document.documentElement.classList.add('dark')
    act(() => {
      root.render(<Probe />)
    })
    expect(readTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('toggle flips the theme, updates the DOM class, and persists to localStorage', () => {
    document.documentElement.classList.add('dark')
    act(() => {
      root.render(<Probe />)
    })
    expect(readTheme()).toBe('dark')

    act(() => {
      click(container.querySelector('[data-testid="toggle"]') as HTMLElement)
    })
    expect(readTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')

    act(() => {
      click(container.querySelector('[data-testid="toggle"]') as HTMLElement)
    })
    expect(readTheme()).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
  })
})
