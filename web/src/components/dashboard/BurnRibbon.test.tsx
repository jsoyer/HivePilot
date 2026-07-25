import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { BurnRibbon } from './BurnRibbon'

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

describe('BurnRibbon', () => {
  it('renders one stacked-area band per category', () => {
    act(() => {
      root.render(
        <BurnRibbon
          series={[
            [1, 2, 3],
            [2, 1, 1],
          ]}
        />,
      )
    })
    expect(container.querySelectorAll('[data-slot="burn-ribbon-band"]').length).toBe(2)
    expect(container.querySelector('[data-slot="burn-ribbon"]')?.getAttribute('role')).toBe('img')
  })

  it('renders a dashed ceiling line when a ceiling is given', () => {
    act(() => {
      root.render(<BurnRibbon series={[[1, 2, 3]]} ceiling={5} />)
    })
    const ceiling = container.querySelector('[data-slot="burn-ribbon-ceiling"]')
    expect(ceiling).not.toBeNull()
    expect(ceiling?.getAttribute('stroke-dasharray')).toBeTruthy()
  })

  it('omits the ceiling line when none is given', () => {
    act(() => {
      root.render(<BurnRibbon series={[[1, 2, 3]]} />)
    })
    expect(container.querySelector('[data-slot="burn-ribbon-ceiling"]')).toBeNull()
  })

  it('renders an emphasized endpoint dot', () => {
    act(() => {
      root.render(<BurnRibbon series={[[1, 2, 3]]} />)
    })
    expect(container.querySelector('[data-slot="burn-ribbon-endpoint"]')).not.toBeNull()
  })

  it('is empty-safe: an empty series array renders a "no data" state without crashing', () => {
    expect(() => {
      act(() => {
        root.render(<BurnRibbon series={[]} />)
      })
    }).not.toThrow()
    const el = container.querySelector('[data-slot="burn-ribbon"]')
    expect(el).not.toBeNull()
    expect(el?.getAttribute('role')).toBe('img')
  })

  it('is empty-safe: series with all-zero-length sub-arrays renders without crashing', () => {
    expect(() => {
      act(() => {
        root.render(<BurnRibbon series={[[], []]} />)
      })
    }).not.toThrow()
  })

  it('is empty-safe: all-zero values render without a NaN path', () => {
    act(() => {
      root.render(<BurnRibbon series={[[0, 0, 0]]} />)
    })
    const band = container.querySelector('[data-slot="burn-ribbon-band"]')
    expect(band?.getAttribute('d')).not.toContain('NaN')
  })
})
