import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AsyncState } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

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

function render(state: AsyncState<{ items: string[] }>) {
  act(() => {
    root.render(
      <AsyncSection
        state={state}
        isEmpty={(data) => data.items.length === 0}
        emptyMessage="No items yet."
      >
        {(data) => <ul data-testid="items">{data.items.join(',')}</ul>}
      </AsyncSection>,
    )
  })
}

describe('AsyncSection', () => {
  it('renders a loading indicator while loading', () => {
    render({ status: 'loading' })
    expect(container.querySelector('[role="status"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="items"]')).toBeNull()
  })

  it('renders an error card on error', () => {
    render({ status: 'error', error: new Error('network down') })
    const alert = container.querySelector('[role="alert"]')
    expect(alert).not.toBeNull()
    expect(alert?.textContent).toContain('network down')
    expect(container.querySelector('[data-testid="items"]')).toBeNull()
  })

  it('renders the empty message when data is empty', () => {
    render({ status: 'success', data: { items: [] } })
    expect(container.textContent).toContain('No items yet.')
    expect(container.querySelector('[data-testid="items"]')).toBeNull()
  })

  it('renders children with the resolved data when non-empty', () => {
    render({ status: 'success', data: { items: ['a', 'b'] } })
    expect(container.querySelector('[data-testid="items"]')?.textContent).toBe('a,b')
  })
})
