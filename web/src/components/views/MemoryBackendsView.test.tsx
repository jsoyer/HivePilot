import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { MemoryBackendsResponse } from '@/lib/pollen-api'

const { fetchMemoryBackends } = vi.hoisted(() => ({ fetchMemoryBackends: vi.fn() }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchMemoryBackends }
})

import { MemoryBackendsView } from './MemoryBackendsView'

let container: HTMLDivElement
let root: Root

const BOTH: MemoryBackendsResponse = {
  days: 30,
  backends: {
    mem0: {
      searches: 150,
      empty_searches: 35,
      stores: 140,
      reads: 0,
      last_activity: '2026-08-10 14:58:10',
      actors: 8,
    },
    obsidian: {
      searches: 0,
      empty_searches: 0,
      stores: 0,
      reads: 0,
      last_activity: null,
      actors: 0,
    },
    hindsight: {
      searches: 0,
      empty_searches: 0,
      stores: 0,
      reads: 0,
      last_activity: null,
      actors: 0,
    },
  },
  egress: { mem0: true, obsidian: false, hindsight: false },
}

async function mountWith(data: MemoryBackendsResponse) {
  fetchMemoryBackends.mockResolvedValue(data)
  await act(async () => {
    root.render(
      <LanguageProvider>
        <MemoryBackendsView />
      </LanguageProvider>,
    )
    await Promise.resolve()
  })
}

describe('MemoryBackendsView', () => {
  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    fetchMemoryBackends.mockReset()
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    window.localStorage.clear()
  })

  it('renders one panel per backend, side by side', async () => {
    await mountWith(BOTH)

    expect(container.querySelector('[data-testid="memory-backend-mem0"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="memory-backend-obsidian"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="memory-backend-hindsight"]')).not.toBeNull()
  })

  it('shows empty recalls, not a raw result count', async () => {
    // 115 of 150 production searches returned exactly 5 — the top-k cap.
    // An average result count would read as quality and mean nothing.
    await mountWith(BOTH)

    const mem0 = container.querySelector('[data-testid="memory-backend-mem0"]')
    expect(mem0?.textContent).toContain('35')
    expect(mem0?.textContent).toContain('150')
  })

  it('states plainly which backend sends work off the host', async () => {
    await mountWith(BOTH)

    const mem0 = container.querySelector('[data-testid="memory-backend-mem0"]')
    const obs = container.querySelector('[data-testid="memory-backend-obsidian"]')
    expect(mem0?.textContent).toMatch(/leaves|third|sort|tiers/i)
    expect(obs?.textContent).toMatch(/host|local|box/i)
  })

  it('renders an idle backend as measured-and-idle, never as absent', async () => {
    // A backend rendered as absent reads as "not applicable". Obsidian sat at
    // zero for months only because nothing recorded it.
    await mountWith(BOTH)

    const obs = container.querySelector('[data-testid="memory-backend-obsidian"]')
    expect(obs).not.toBeNull()
    expect(obs?.textContent).toContain('0')
  })

  it('does not present the two as interchangeable', async () => {
    // Measured: their recalls overlap by 2–4%. A screen implying one replaces
    // the other would invite cutting a live capability.
    await mountWith(BOTH)

    expect(container.textContent).toMatch(/complement|comparabl|not interchangeable|se recoupent/i)
  })
})
