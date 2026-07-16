import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { DurationStats, TrendPoint } from '@/lib/mirador-api'
import { PercentileBars, TrendBarChart } from './charts'

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

const series: TrendPoint[] = [
  { bucket: '2026-07-01', total: 3, outcomes: { succeeded: 2, failed: 1, skipped: 0, other: 0 } },
  { bucket: '2026-07-02', total: 5, outcomes: { succeeded: 4, failed: 1, skipped: 0, other: 0 } },
]

const stats: DurationStats = { count: 10, min: 0.1, max: 5, avg: 1.2, p50: 1, p95: 3, p99: 4.5 }

describe('TrendBarChart', () => {
  it('renders one bar per bucket', () => {
    act(() => {
      root.render(<TrendBarChart series={series} />)
    })
    const bars = container.querySelectorAll('svg rect')
    expect(bars.length).toBe(2)
  })

  it('includes per-bucket detail (bucket key + counts) discoverable via title', () => {
    act(() => {
      root.render(<TrendBarChart series={series} />)
    })
    expect(container.textContent).toContain('2026-07-01')
    expect(container.textContent).toContain('2026-07-02')
  })
})

describe('PercentileBars', () => {
  it('renders p50/p95/p99 labels with their values', () => {
    act(() => {
      root.render(<PercentileBars stats={stats} />)
    })
    expect(container.textContent).toContain('p50')
    expect(container.textContent).toContain('p95')
    expect(container.textContent).toContain('p99')
    expect(container.textContent).toContain('1.00s')
    expect(container.textContent).toContain('3.00s')
    expect(container.textContent).toContain('4.50s')
  })
})
