import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MetricReadout } from './MetricReadout'
import type { VizTone } from './Sparkline'

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

describe('MetricReadout', () => {
  it('renders the label, value, and sub line', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} sub="last 30 days" />)
    })
    expect(container.textContent).toContain('Total runs')
    expect(container.textContent).toContain('42')
    expect(container.textContent).toContain('last 30 days')
  })

  it('renders the value in the mono tabular-numeral treatment', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} />)
    })
    const value = container.querySelector('[data-slot="metric-readout-value"]')
    expect(value?.className).toContain('metric-mono')
  })

  it('renders a baseline rule under the value', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} />)
    })
    expect(container.querySelector('[data-slot="metric-readout-rule"]')).not.toBeNull()
  })

  it('renders an upward trend tinted good, with the arrow and label', () => {
    act(() => {
      root.render(
        <MetricReadout label="Success rate" value="82%" trend={{ direction: 'up', label: '+4% vs last week' }} />,
      )
    })
    const trend = container.querySelector('[data-slot="metric-readout-trend"]')
    expect(trend?.textContent).toContain('▲')
    expect(trend?.textContent).toContain('+4% vs last week')
    expect(trend?.className).toContain('--color-good')
  })

  it('renders a downward trend tinted crit by default', () => {
    act(() => {
      root.render(<MetricReadout label="Failures" value={3} trend={{ direction: 'down', label: '-1' }} />)
    })
    const trend = container.querySelector('[data-slot="metric-readout-trend"]')
    expect(trend?.textContent).toContain('▼')
    expect(trend?.className).toContain('--color-crit')
  })

  it('respects an explicit trend tone override', () => {
    act(() => {
      root.render(
        <MetricReadout label="Cost" value="$1.23" trend={{ direction: 'up', label: '+$0.10', tone: 'crit' }} />,
      )
    })
    expect(container.querySelector('[data-slot="metric-readout-trend"]')?.className).toContain('--color-crit')
  })

  it('omits the sub line and trend when neither is provided', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} />)
    })
    expect(container.querySelector('[data-slot="metric-readout-sub"]')).toBeNull()
    expect(container.querySelector('[data-slot="metric-readout-trend"]')).toBeNull()
  })

  it('renders the icon slot when an icon is given', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} icon={<svg data-testid="icon" />} />)
    })
    expect(container.querySelector('[data-testid="icon"]')).not.toBeNull()
  })

  it.each<VizTone>(['default', 'good', 'warn', 'crit'])('applies the %s tone via data-tone', (tone) => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} tone={tone} />)
    })
    expect(container.querySelector(`[data-slot="metric-readout"][data-tone="${tone}"]`)).not.toBeNull()
  })

  it('defaults to the "default" tone when none is given', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} />)
    })
    expect(container.querySelector('[data-slot="metric-readout"][data-tone="default"]')).not.toBeNull()
  })

  it('renders a left severity stripe for non-nominal tones (good/warn/crit), but not for the default tone', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} tone="default" />)
    })
    const defaultCard = container.querySelector('[data-slot="metric-readout"]')
    expect(defaultCard?.className).not.toMatch(/border-l-2/)

    for (const tone of ['good', 'warn', 'crit'] as const) {
      act(() => {
        root.render(<MetricReadout label="Total runs" value={42} tone={tone} />)
      })
      const card = container.querySelector('[data-slot="metric-readout"]')
      expect(card?.className).toContain('border-l-2')
      expect(card?.className).toContain(`--color-${tone}`)
    }
  })
})
