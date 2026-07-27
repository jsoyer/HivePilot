import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Drawer } from './drawer'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('Drawer', () => {
  it('renders a labelled dialog with its title and body', () => {
    act(() => {
      root.render(
        <Drawer title="Run #7" closeLabel="Close" onClose={() => {}}>
          body content
        </Drawer>,
      )
    })
    const dialog = container.querySelector('[role="dialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog?.getAttribute('aria-label')).toBe('Run #7')
    expect(container.textContent).toContain('body content')
  })

  it('closes on the close button', () => {
    const onClose = vi.fn()
    act(() => {
      root.render(
        <Drawer title="t" closeLabel="Close it" onClose={onClose}>
          x
        </Drawer>,
      )
    })
    const button = container.querySelector('[aria-label="Close it"]') as HTMLButtonElement
    act(() => {
      button.click()
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on the backdrop', () => {
    const onClose = vi.fn()
    act(() => {
      root.render(
        <Drawer title="t" closeLabel="c" onClose={onClose}>
          x
        </Drawer>,
      )
    })
    const backdrop = container.querySelector('[data-slot="drawer-backdrop"]') as HTMLElement
    act(() => {
      backdrop.click()
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on Escape', () => {
    const onClose = vi.fn()
    act(() => {
      root.render(
        <Drawer title="t" closeLabel="c" onClose={onClose}>
          x
        </Drawer>,
      )
    })
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('hides the backdrop from assistive tech', () => {
    act(() => {
      root.render(
        <Drawer title="t" closeLabel="c" onClose={() => {}}>
          x
        </Drawer>,
      )
    })
    expect(
      container.querySelector('[data-slot="drawer-backdrop"]')?.getAttribute('aria-hidden'),
    ).toBe('true')
  })

  it('scrolls its own body rather than the page', () => {
    act(() => {
      root.render(
        <Drawer title="t" closeLabel="c" onClose={() => {}}>
          x
        </Drawer>,
      )
    })
    expect(container.querySelector('[role="dialog"]')?.className).toMatch(/overflow-y-auto/)
  })
})
