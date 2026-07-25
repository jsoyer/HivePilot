import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Sparkline } from './Sparkline'

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

describe('Sparkline', () => {
  it('renders an svg with an aria-label summarizing the series', () => {
    act(() => {
      root.render(<Sparkline points={[1, 5, 3, 8, 2]} />)
    })
    const svg = container.querySelector('[data-slot="sparkline"]')
    expect(svg).not.toBeNull()
    expect(svg?.getAttribute('role')).toBe('img')
    expect(svg?.getAttribute('aria-label')).toBeTruthy()
  })

  it('accepts a custom aria-label', () => {
    act(() => {
      root.render(<Sparkline points={[1, 2, 3]} ariaLabel="Custom summary" />)
    })
    expect(container.querySelector('[data-slot="sparkline"]')?.getAttribute('aria-label')).toBe('Custom summary')
  })

  it('draws the endpoint dot at the last data point', () => {
    act(() => {
      root.render(<Sparkline points={[1, 2, 3]} />)
    })
    expect(container.querySelector('[data-slot="sparkline"] circle')).not.toBeNull()
  })

  it('is empty-safe: an empty points array renders without crashing (and a non-svg placeholder)', () => {
    expect(() => {
      act(() => {
        root.render(<Sparkline points={[]} />)
      })
    }).not.toThrow()
    const el = container.querySelector('[data-slot="sparkline"]')
    expect(el).not.toBeNull()
    expect(el?.getAttribute('role')).toBe('img')
  })

  it('is empty-safe: a single point renders without crashing', () => {
    expect(() => {
      act(() => {
        root.render(<Sparkline points={[42]} />)
      })
    }).not.toThrow()
  })

  it('is empty-safe: a flat series (all equal values) never produces a NaN path', () => {
    act(() => {
      root.render(<Sparkline points={[5, 5, 5, 5]} />)
    })
    const path = container.querySelector('[data-slot="sparkline"] path')
    expect(path?.getAttribute('d')).not.toContain('NaN')
  })

  it.each(['default', 'good', 'warn', 'crit'] as const)('renders without crashing for tone=%s', (tone) => {
    act(() => {
      root.render(<Sparkline points={[1, 2, 3]} tone={tone} />)
    })
    expect(container.querySelector('[data-slot="sparkline"]')).not.toBeNull()
  })
})
