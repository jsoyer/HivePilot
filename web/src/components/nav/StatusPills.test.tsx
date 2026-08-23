import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PluginsHealthResponse } from '@/lib/pollen-api'

const mocks = vi.hoisted(() => ({
  fetchPluginsHealth: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
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

  it('shows ONLY active (ok) plugins — degraded and error never reach the header', async () => {
    // The operator decision this pins: the header strip answers "what is
    // live", nothing else. Five default-enabled-but-unused plugins once
    // filled it with red; the full tri-state picture belongs to the Health
    // tab, which the header never replaces.
    const data: PluginsHealthResponse = {
      plugins: [
        { name: 'store', status: 'ok', detail: '', activity_available: false, activity: null },
        { name: 'mem0', status: 'ok', detail: '', activity_available: false, activity: null },
        {
          name: 'headroom',
          status: 'degraded',
          detail: 'slow',
          activity_available: false,
          activity: null,
        },
        {
          name: 'broken',
          status: 'error',
          detail: 'down',
          activity_available: false,
          activity: null,
        },
      ],
      disabled: [],
    }
    mocks.fetchPluginsHealth.mockResolvedValue(data)
    await mount()

    const pills = Array.from(container.querySelectorAll('[data-testid="status-pill"]'))
    expect(pills).toHaveLength(2)
    const text = container.querySelector('[data-testid="status-pills"]')?.textContent ?? ''
    expect(text).toContain('store')
    expect(text).toContain('mem0')
    expect(text).not.toContain('headroom')
    expect(text).not.toContain('broken')
  })

  it('renders nothing when no plugin is active — an empty strip, not a strip of problems', async () => {
    const data: PluginsHealthResponse = {
      plugins: [
        {
          name: 'headroom',
          status: 'degraded',
          detail: '',
          activity_available: false,
          activity: null,
        },
        {
          name: 'broken',
          status: 'error',
          detail: '',
          activity_available: false,
          activity: null,
        },
      ],
      disabled: [],
    }
    mocks.fetchPluginsHealth.mockResolvedValue(data)
    await mount()
    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
  })

  it('visual identity: every rendered pill pulses its live dot and contributes no dot text', async () => {
    const data: PluginsHealthResponse = {
      plugins: [
        { name: 'store', status: 'ok', detail: '', activity_available: false, activity: null },
      ],
      disabled: [],
    }
    mocks.fetchPluginsHealth.mockResolvedValue(data)
    await mount()

    const dot = container.querySelector('[data-testid="status-pill-dot"]')
    expect(dot?.className).toContain('animate-pulse')
    // The dot never contributes text content — the pill's accessible name
    // stays exactly "<name> <status>".
    expect(dot?.textContent).toBe('')
  })
})
