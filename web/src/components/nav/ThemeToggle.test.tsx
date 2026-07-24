import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { THEME_STORAGE_KEY } from '@/lib/use-theme'
import { ThemeToggle } from './ThemeToggle'

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

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('ThemeToggle', () => {
  it('renders a single accessible button', () => {
    act(() => {
      root.render(<ThemeToggle />)
    })
    const button = container.querySelector('button')
    expect(button).not.toBeNull()
    expect(button?.getAttribute('aria-label')).toBeTruthy()
  })

  it('flips the .dark class on <html> and persists the choice when clicked', () => {
    document.documentElement.classList.add('dark')
    act(() => {
      root.render(<ThemeToggle />)
    })
    const button = container.querySelector('button') as HTMLElement

    act(() => {
      click(button)
    })
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')

    act(() => {
      click(button)
    })
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
  })

  it('updates its own aria-label to describe the NEXT action after toggling', () => {
    document.documentElement.classList.add('dark')
    act(() => {
      root.render(<ThemeToggle />)
    })
    const button = container.querySelector('button') as HTMLElement
    const labelWhenDark = button.getAttribute('aria-label')

    act(() => {
      click(button)
    })
    const labelWhenLight = button.getAttribute('aria-label')
    expect(labelWhenLight).not.toBe(labelWhenDark)
  })
})
