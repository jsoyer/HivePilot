import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Gauge } from './Gauge'

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

describe('Gauge', () => {
  it('renders an svg with an aria-label and a mono readout of the value', () => {
    act(() => {
      root.render(<Gauge value={0.42} label="Cache hit rate" />)
    })
    const svg = container.querySelector('[data-slot="gauge"]')
    expect(svg).not.toBeNull()
    expect(svg?.getAttribute('role')).toBe('img')
    expect(svg?.getAttribute('aria-label')).toContain('42%')
    expect(container.querySelector('[data-slot="gauge-readout"]')?.textContent).toContain('42%')
    expect(container.textContent).toContain('Cache hit rate')
  })

  it('is zero-safe: value 0 renders without crashing and omits the (empty) value arc', () => {
    act(() => {
      root.render(<Gauge value={0} />)
    })
    // A 0-length arc has no path to draw — omitted entirely rather than a
    // degenerate/NaN path.
    expect(container.querySelector('[data-slot="gauge-value-arc"]')).toBeNull()
    expect(container.querySelector('[data-slot="gauge-track"]')?.getAttribute('d')).not.toContain('NaN')
  })

  it('clamps out-of-range values into [0,1]', () => {
    act(() => {
      root.render(<Gauge value={1.7} />)
    })
    expect(container.querySelector('[data-slot="gauge-readout"]')?.textContent).toContain('100%')
  })

  it('clamps negative values to 0', () => {
    act(() => {
      root.render(<Gauge value={-0.3} />)
    })
    expect(container.querySelector('[data-slot="gauge-readout"]')?.textContent).toContain('0%')
  })

  it('renders a target tick when a target is given', () => {
    act(() => {
      root.render(<Gauge value={0.5} target={0.8} />)
    })
    expect(container.querySelector('[data-slot="gauge-target-tick"]')).not.toBeNull()
  })

  it('omits the target tick when no target is given', () => {
    act(() => {
      root.render(<Gauge value={0.5} />)
    })
    expect(container.querySelector('[data-slot="gauge-target-tick"]')).toBeNull()
  })

  it.each(['default', 'good', 'warn', 'crit'] as const)('renders without crashing for tone=%s', (tone) => {
    act(() => {
      root.render(<Gauge value={0.6} tone={tone} />)
    })
    expect(container.querySelector('[data-slot="gauge"]')).not.toBeNull()
  })
})
