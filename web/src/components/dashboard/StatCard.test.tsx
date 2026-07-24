import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { StatCard, type StatCardTone } from './StatCard'

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

describe('StatCard', () => {
  it('renders the label, value, and sub line', () => {
    act(() => {
      root.render(<StatCard label="Total runs" value={42} sub="last 30 days" />)
    })
    expect(container.textContent).toContain('Total runs')
    expect(container.textContent).toContain('42')
    expect(container.textContent).toContain('last 30 days')
  })

  it('omits the sub line when not provided', () => {
    act(() => {
      root.render(<StatCard label="Total runs" value={42} />)
    })
    expect(container.querySelector('[data-slot="stat-card-sub"]')).toBeNull()
  })

  it('renders the icon slot when an icon is given', () => {
    act(() => {
      root.render(<StatCard label="Total runs" value={42} icon={<svg data-testid="icon" />} />)
    })
    expect(container.querySelector('[data-testid="icon"]')).not.toBeNull()
  })

  it('omits the icon chip when no icon is given', () => {
    act(() => {
      root.render(<StatCard label="Total runs" value={42} />)
    })
    expect(container.querySelector('[data-slot="stat-card-icon"]')).toBeNull()
  })

  it.each<StatCardTone>(['default', 'positive', 'warning', 'danger'])(
    'applies the %s tone via data-tone',
    (tone) => {
      act(() => {
        root.render(<StatCard label="Total runs" value={42} tone={tone} />)
      })
      expect(container.querySelector(`[data-slot="stat-card"][data-tone="${tone}"]`)).not.toBeNull()
    }
  )

  it('defaults to the "default" tone when none is given', () => {
    act(() => {
      root.render(<StatCard label="Total runs" value={42} />)
    })
    expect(container.querySelector('[data-slot="stat-card"][data-tone="default"]')).not.toBeNull()
  })

  it('applies an uppercase style to the label', () => {
    act(() => {
      root.render(<StatCard label="Total runs" value={42} />)
    })
    const label = container.querySelector('[data-slot="stat-card-label"]')
    expect(label?.className).toContain('uppercase')
  })
})
