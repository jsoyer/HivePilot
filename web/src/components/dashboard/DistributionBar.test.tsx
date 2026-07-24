import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DistributionBar, type DistributionSegment } from './DistributionBar'

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

const segments: DistributionSegment[] = [
  { key: 'succeeded', label: 'Succeeded', value: 9 },
  { key: 'failed', label: 'Failed', value: 2 },
  { key: 'other', label: 'Other', value: 1 },
]

describe('DistributionBar', () => {
  it('renders one bar segment per non-zero value, sized proportionally to the total', () => {
    act(() => {
      root.render(<DistributionBar segments={segments} />)
    })
    const rendered = container.querySelectorAll('[data-slot="distribution-bar-segment"]')
    expect(rendered.length).toBe(3)

    const succeeded = container.querySelector('[data-key="succeeded"]')
    expect(succeeded?.getAttribute('data-percent')).toBe('75.00')
    expect((succeeded as HTMLElement)?.style.width).toBe('75%')

    const failed = container.querySelector('[data-key="failed"]')
    expect(failed?.getAttribute('data-percent')).toBe('16.67')
  })

  it('renders a legend row with a colored dot, label, and count per segment', () => {
    act(() => {
      root.render(<DistributionBar segments={segments} />)
    })
    const dots = container.querySelectorAll('[data-slot="distribution-bar-dot"]')
    expect(dots.length).toBe(3)
    expect(container.textContent).toContain('Succeeded')
    expect(container.textContent).toContain('9')
    expect(container.textContent).toContain('Failed')
    expect(container.textContent).toContain('2')
    expect(container.textContent).toContain('Other')
    expect(container.textContent).toContain('1')
  })

  it('cycles through a default palette when colorClass is not given', () => {
    act(() => {
      root.render(<DistributionBar segments={segments} />)
    })
    const dots = container.querySelectorAll('[data-slot="distribution-bar-dot"]')
    const classNames = Array.from(dots).map((dot) => dot.className)
    expect(new Set(classNames).size).toBeGreaterThan(1)
  })

  it('respects an explicit colorClass override', () => {
    act(() => {
      root.render(
        <DistributionBar
          segments={[{ key: 'a', label: 'A', value: 1, colorClass: 'bg-emerald-500' }]}
        />
      )
    })
    const segment = container.querySelector('[data-key="a"]')
    expect(segment?.className).toContain('bg-emerald-500')
  })

  it('shows a muted empty state when all segment values are zero', () => {
    act(() => {
      root.render(<DistributionBar segments={[{ key: 'a', label: 'A', value: 0 }]} />)
    })
    expect(container.querySelectorAll('[data-slot="distribution-bar-segment"]').length).toBe(0)
    expect(container.textContent).toMatch(/no data/i)
  })

  it('shows a muted empty state when there are no segments at all', () => {
    act(() => {
      root.render(<DistributionBar segments={[]} />)
    })
    expect(container.textContent).toMatch(/no data/i)
  })

  it('provides an aria-label summarizing the breakdown', () => {
    act(() => {
      root.render(<DistributionBar segments={segments} />)
    })
    const img = container.querySelector('[role="img"]')
    expect(img?.getAttribute('aria-label')).toMatch(/succeeded/i)
    expect(img?.getAttribute('aria-label')).toMatch(/75%/)
  })
})
