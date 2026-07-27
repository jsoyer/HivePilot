import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from './table'

let container: HTMLDivElement
let root: Root

function renderTable(scrollLabel?: string) {
  act(() => {
    root.render(
      <Table scrollLabel={scrollLabel}>
        <TableHeader>
          <TableRow>
            <TableHead>Model</TableHead>
            <TableHead>Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>claude-opus-4-5</TableCell>
            <TableCell>$150.05</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )
  })
}

function scroller() {
  return container.querySelector('[data-slot="table-container"]') as HTMLElement
}

/** jsdom lays nothing out, so overflow has to be simulated. */
function setWidths(el: HTMLElement, scrollWidth: number, clientWidth: number) {
  Object.defineProperty(el, 'scrollWidth', { value: scrollWidth, configurable: true })
  Object.defineProperty(el, 'clientWidth', { value: clientWidth, configurable: true })
  act(() => {
    window.dispatchEvent(new Event('resize'))
  })
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

describe('Table', () => {
  it('renders the table inside a container that owns its own horizontal overflow', () => {
    renderTable()
    // This is what keeps a wide table from scrolling the page body sideways.
    expect(scroller().className).toContain('overflow-x-auto')
    expect(scroller().querySelector('table')).not.toBeNull()
  })

  it('passes the className through to the table, not the container', () => {
    act(() => {
      root.render(<Table className="block xl:table" />)
    })
    expect(container.querySelector('table')!.className).toContain('block xl:table')
    expect(scroller().className).not.toContain('xl:table')
  })

  // Mobile audit: the container was neither focusable nor labelled, so the
  // 43-53% of each wide metric table that sits off-screen at 390px was
  // unreachable by keyboard and undiscoverable by sight.
  describe('when the content overflows', () => {
    it('becomes a focusable scroll region', () => {
      renderTable('Models table')
      setWidths(scroller(), 660, 334)
      expect(scroller().getAttribute('data-scrollable')).toBe('true')
      expect(scroller().getAttribute('tabindex')).toBe('0')
      expect(scroller().getAttribute('role')).toBe('region')
      expect(scroller().getAttribute('aria-label')).toBe('Models table')
    })

    it('stays focusable but unnamed when no scrollLabel is supplied', () => {
      renderTable()
      setWidths(scroller(), 660, 334)
      expect(scroller().getAttribute('tabindex')).toBe('0')
      // An unnamed role="region" is worse than none at all.
      expect(scroller().getAttribute('role')).toBeNull()
    })

    it('carries the shared slim-scrollbar affordance class', () => {
      renderTable()
      expect(scroller().className).toContain('table-scroll')
    })
  })

  // The flip side, and the reason this is measured rather than always-on: a
  // permanent tab stop on every table on every desktop would be a regression.
  describe('when the content fits', () => {
    it('CRITICAL: adds no tab stop and no region role', () => {
      renderTable('Models table')
      setWidths(scroller(), 334, 334)
      expect(scroller().getAttribute('data-scrollable')).toBe('false')
      expect(scroller().getAttribute('tabindex')).toBeNull()
      expect(scroller().getAttribute('role')).toBeNull()
      expect(scroller().getAttribute('aria-label')).toBeNull()
    })
  })
})
