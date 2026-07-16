import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Mirador } from './Mirador'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => {
    root.render(<Mirador />)
  })
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
})

describe('Mirador', () => {
  it('renders the Mirador title and all four tabs', () => {
    expect(container.textContent).toContain('Mirador')
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(['Analytics', 'Cost', 'Health', 'Mem0'])
  })

  it('shows the Analytics placeholder panel by default', () => {
    expect(container.textContent).toContain('Wired in Sprint 3')
    const analyticsTab = container.querySelector('[role="tab"]')
    expect(analyticsTab?.getAttribute('aria-selected')).toBe('true')
  })

  it('switches to the Cost panel when the Cost tab is clicked', () => {
    const costTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement

    act(() => {
      costTab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      costTab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(costTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Cost')
  })
})
