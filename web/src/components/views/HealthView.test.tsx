import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PluginsHealthResponse } from '@/lib/mirador-api'

const { fetchPluginsHealth } = vi.hoisted(() => ({ fetchPluginsHealth: vi.fn() }))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchPluginsHealth }
})

import { HealthView } from './HealthView'

let container: HTMLDivElement
let root: Root

const health: PluginsHealthResponse = {
  plugins: [
    { name: 'rtk', status: 'ok', detail: 'reachable' },
    { name: 'mem0', status: 'degraded', detail: 'self-hosted, slow' },
    { name: 'obsidian', status: 'error', detail: 'vault path missing' },
  ],
}

function mount() {
  act(() => {
    root.render(<HealthView />)
  })
}

beforeEach(() => {
  fetchPluginsHealth.mockReset()
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

describe('HealthView', () => {
  it('shows a loading indicator before data resolves', () => {
    fetchPluginsHealth.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('renders one badge per plugin with its status and detail', async () => {
    fetchPluginsHealth.mockResolvedValue(health)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('rtk')
    expect(container.textContent).toContain('ok')
    expect(container.textContent).toContain('mem0')
    expect(container.textContent).toContain('degraded')
    expect(container.textContent).toContain('obsidian')
    expect(container.textContent).toContain('error')
    expect(container.textContent).toContain('vault path missing')
  })

  it('shows an empty state when no plugins are registered', async () => {
    fetchPluginsHealth.mockResolvedValue({ plugins: [] } satisfies PluginsHealthResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no plugins/i)
  })

  it('shows an error card when the endpoint rejects', async () => {
    fetchPluginsHealth.mockRejectedValue(new Error('unreachable'))

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('unreachable')
  })
})
