import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { EmptyState } from './EmptyState'

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
})

describe('EmptyState', () => {
  it('renders the title and the explanation of what would fill it', () => {
    act(() => {
      root.render(<EmptyState title="Queue empty" body="Autopilot queues an objective when…" />)
    })
    expect(container.textContent).toContain('Queue empty')
    expect(container.textContent).toContain('Autopilot queues an objective when…')
  })

  it('renders the action slot when one is supplied', () => {
    act(() => {
      root.render(
        <EmptyState title="t" body="b" action={<button type="button">Configure a budget</button>} />,
      )
    })
    expect(container.querySelector('button')?.textContent).toBe('Configure a budget')
  })

  it('omits the action row entirely when there is no action', () => {
    act(() => {
      root.render(<EmptyState title="t" body="b" />)
    })
    expect(container.querySelector('[data-slot="empty-state-action"]')).toBeNull()
  })

  it('hides the decorative icon from assistive tech', () => {
    act(() => {
      root.render(<EmptyState title="t" body="b" icon={<svg />} />)
    })
    expect(container.querySelector('[data-slot="empty-state-icon"]')?.getAttribute('aria-hidden')).toBe(
      'true',
    )
  })
})
