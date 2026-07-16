import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import type { PanelData } from '@/lib/mirador-api'

const { fetchPanel } = vi.hoisted(() => ({ fetchPanel: vi.fn() }))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchPanel }
})

import { PanelView } from './PanelView'

let container: HTMLDivElement
let root: Root

function mount(props: { name: string; title: string; minRole: string }) {
  act(() => {
    root.render(<PanelView {...props} />)
  })
}

beforeEach(() => {
  fetchPanel.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('PanelView', () => {
  it('shows a loading indicator before data resolves', () => {
    fetchPanel.mockReturnValue(new Promise(() => {}))
    mount({ name: 'rtk-status', title: 'RTK Status', minRole: 'read' })
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('fetches the named panel and renders it via PanelRenderer', async () => {
    fetchPanel.mockResolvedValue({
      sections: [{ kind: 'stat', label: 'Queue', value: '2', status: 'ok' }],
    } satisfies PanelData)

    await act(async () => {
      mount({ name: 'rtk-status', title: 'RTK Status', minRole: 'read' })
      await Promise.resolve()
    })

    expect(fetchPanel).toHaveBeenCalledWith('rtk-status')
    expect(container.textContent).toContain('Queue')
    expect(container.textContent).toContain('2')
  })

  it('shows an empty state when the panel has no sections', async () => {
    fetchPanel.mockResolvedValue({ sections: [] } satisfies PanelData)

    await act(async () => {
      mount({ name: 'empty-panel', title: 'Empty Panel', minRole: 'read' })
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no data/i)
  })

  it('shows a generic error card for a non-403 failure', async () => {
    fetchPanel.mockRejectedValue(new Error('network down'))

    await act(async () => {
      mount({ name: 'flaky-panel', title: 'Flaky Panel', minRole: 'read' })
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toContain('network down')
  })

  it('CRITICAL: shows a graceful "requires a <role> token" message on 403 — not a crash or generic error', async () => {
    fetchPanel.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount({ name: 'secure-panel', title: 'Secure Panel', minRole: 'admin' })
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).toBeNull()
    const forbidden = container.querySelector('[data-testid="panel-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(forbidden?.textContent).toMatch(/admin/i)
    expect(container.textContent).not.toMatch(/something went wrong/i)
  })
})
