import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PluginsHealthResponse } from '@/lib/mirador-api'

const mocks = vi.hoisted(() => ({
  fetchPluginsHealth: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, ...mocks }
})

import { StatusPills } from './StatusPills'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  mocks.fetchPluginsHealth.mockReset()
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

async function mount() {
  await act(async () => {
    root.render(<StatusPills />)
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('StatusPills', () => {
  it('renders nothing while loading (no crash, no flash of empty pills)', () => {
    mocks.fetchPluginsHealth.mockReturnValue(new Promise(() => {}))
    act(() => {
      root.render(<StatusPills />)
    })
    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
  })

  it('renders nothing when the health fetch fails (never crashes the header)', async () => {
    mocks.fetchPluginsHealth.mockRejectedValue(new Error('boom'))
    await mount()
    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
    expect(container.textContent).toBe('')
  })

  it('renders nothing when there are no plugins', async () => {
    const empty: PluginsHealthResponse = { plugins: [], disabled: [] }
    mocks.fetchPluginsHealth.mockResolvedValue(empty)
    await mount()
    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
  })

  it('renders one pill per plugin with its name and status', async () => {
    const data: PluginsHealthResponse = {
      plugins: [
        { name: 'store', status: 'ok', detail: '' },
        { name: 'mem0', status: 'ok', detail: '' },
        { name: 'headroom', status: 'degraded', detail: 'slow' },
      ],
      disabled: [],
    }
    mocks.fetchPluginsHealth.mockResolvedValue(data)
    await mount()

    const pillsContainer = container.querySelector('[data-testid="status-pills"]')
    expect(pillsContainer).not.toBeNull()
    expect(pillsContainer?.textContent).toContain('store')
    expect(pillsContainer?.textContent).toContain('ok')
    expect(pillsContainer?.textContent).toContain('headroom')
    expect(pillsContainer?.textContent).toContain('degraded')
  })

  it('styles a plugin pill by its status (ok/degraded/error map to distinct classes)', async () => {
    const data: PluginsHealthResponse = {
      plugins: [
        { name: 'store', status: 'ok', detail: '' },
        { name: 'broken', status: 'error', detail: 'down' },
      ],
      disabled: [],
    }
    mocks.fetchPluginsHealth.mockResolvedValue(data)
    await mount()

    const pills = Array.from(container.querySelectorAll('[data-testid="status-pill"]'))
    expect(pills).toHaveLength(2)
    const okPill = pills.find((p) => p.textContent?.includes('store'))
    const errorPill = pills.find((p) => p.textContent?.includes('broken'))
    expect(okPill?.className).not.toEqual(errorPill?.className)
  })
})
