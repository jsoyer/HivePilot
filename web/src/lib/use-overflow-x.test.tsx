import { act, useRef } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useOverflowX } from './use-overflow-x'

let container: HTMLDivElement
let root: Root

/** Drives the hook against a real element whose scroll/client widths we can
 * control, since jsdom never lays anything out on its own. */
function Harness({ onState }: { onState: (overflowing: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const overflowing = useOverflowX(ref)
  onState(overflowing)
  return <div ref={ref} data-testid="scroller" />
}

function setWidths(el: HTMLElement, scrollWidth: number, clientWidth: number) {
  Object.defineProperty(el, 'scrollWidth', { value: scrollWidth, configurable: true })
  Object.defineProperty(el, 'clientWidth', { value: clientWidth, configurable: true })
}

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

describe('useOverflowX', () => {
  it('reports false when the content fits', () => {
    const states: boolean[] = []
    act(() => {
      root.render(<Harness onState={(s) => states.push(s)} />)
    })
    const el = container.querySelector('[data-testid="scroller"]') as HTMLElement
    setWidths(el, 300, 300)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(false)
  })

  it('reports true once the content is wider than the box', () => {
    const states: boolean[] = []
    act(() => {
      root.render(<Harness onState={(s) => states.push(s)} />)
    })
    const el = container.querySelector('[data-testid="scroller"]') as HTMLElement
    setWidths(el, 717, 334)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(true)
  })

  // Sub-pixel layout rounding routinely makes scrollWidth exceed clientWidth
  // by a fraction. Treating that as "scrollable" would put a useless tab stop
  // on every table on every desktop.
  it('ignores a sub-pixel difference', () => {
    const states: boolean[] = []
    act(() => {
      root.render(<Harness onState={(s) => states.push(s)} />)
    })
    const el = container.querySelector('[data-testid="scroller"]') as HTMLElement
    setWidths(el, 334.4, 334)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(false)
  })

  // Measured case: Cost's by-project table overhangs by exactly 2px at 390px.
  // No perceptible content is cut, so it must not become an announced region.
  it('ignores an overhang too small to hide any content', () => {
    const states: boolean[] = []
    act(() => {
      root.render(<Harness onState={(s) => states.push(s)} />)
    })
    const el = container.querySelector('[data-testid="scroller"]') as HTMLElement
    setWidths(el, 336, 334)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(false)
  })

  // ...while the tables that genuinely need it overhang by 250-383px.
  it('still reports a real overhang just above the tolerance', () => {
    const states: boolean[] = []
    act(() => {
      root.render(<Harness onState={(s) => states.push(s)} />)
    })
    const el = container.querySelector('[data-testid="scroller"]') as HTMLElement
    setWidths(el, 344, 334)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(true)
  })

  it('flips back to false when the viewport grows enough to fit the content', () => {
    const states: boolean[] = []
    act(() => {
      root.render(<Harness onState={(s) => states.push(s)} />)
    })
    const el = container.querySelector('[data-testid="scroller"]') as HTMLElement

    setWidths(el, 717, 334)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(true)

    setWidths(el, 717, 900)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(states.at(-1)).toBe(false)
  })

  it('does not throw when the ref is never attached', () => {
    function Detached() {
      const ref = useRef<HTMLDivElement>(null)
      useOverflowX(ref)
      return null
    }
    expect(() => {
      act(() => {
        root.render(<Detached />)
      })
      act(() => {
        window.dispatchEvent(new Event('resize'))
      })
    }).not.toThrow()
  })
})
