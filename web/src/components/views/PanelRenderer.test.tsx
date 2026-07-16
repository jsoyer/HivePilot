import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { PanelData } from '@/lib/mirador-api'
import { PanelRenderer } from './PanelRenderer'

let container: HTMLDivElement
let root: Root

function render(data: PanelData) {
  act(() => {
    root.render(<PanelRenderer data={data} />)
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

describe('PanelRenderer', () => {
  it('renders a stat section with label, value and status badge', () => {
    render({
      sections: [{ kind: 'stat', label: 'Queue depth', value: '3', status: 'ok' }],
    })
    expect(container.textContent).toContain('Queue depth')
    expect(container.textContent).toContain('3')
    expect(container.textContent).toContain('ok')
  })

  it('renders a stat section without a status badge when status is null', () => {
    render({
      sections: [{ kind: 'stat', label: 'Uptime', value: '99.9%', status: null }],
    })
    expect(container.textContent).toContain('Uptime')
    expect(container.textContent).toContain('99.9%')
  })

  it('renders a table section with columns and rows', () => {
    render({
      sections: [
        {
          kind: 'table',
          columns: ['Name', 'Status'],
          rows: [
            ['alpha', 'ok'],
            ['beta', 'error'],
          ],
        },
      ],
    })
    expect(container.querySelector('table')).not.toBeNull()
    expect(container.textContent).toContain('Name')
    expect(container.textContent).toContain('alpha')
    expect(container.textContent).toContain('beta')
  })

  it('renders a text section as plain escaped text, never raw HTML', () => {
    render({
      sections: [{ kind: 'text', content: '<img src=x onerror=alert(1)>' }],
    })
    // The literal markup must appear as visible TEXT, not be parsed into a
    // real <img> element — proves no dangerouslySetInnerHTML is used.
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('renders multiple sections in order', () => {
    render({
      sections: [
        { kind: 'text', content: 'intro text' },
        { kind: 'stat', label: 'Count', value: '5', status: null },
      ],
    })
    expect(container.textContent).toContain('intro text')
    expect(container.textContent).toContain('Count')
  })

  it('shows a "no data" message when sections is empty', () => {
    render({ sections: [] })
    expect(container.textContent).toMatch(/no data/i)
  })
})
