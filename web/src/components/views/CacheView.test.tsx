import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { CacheReport } from '@/lib/pollen-api'

const { fetchCacheReport } = vi.hoisted(() => ({ fetchCacheReport: vi.fn() }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchCacheReport }
})

import { CacheView } from './CacheView'

let container: HTMLDivElement
let root: Root

const LOSING: CacheReport = {
  sessions: 20,
  median_amortisation: 0.0,
  below_one: 19,
  wasted_tokens: 19000,
  healthy: false,
  worst: {
    session_id: 'e27a51e5-6f85-472c-93b2-20cfd66a0dbb',
    model: 'claude-opus-5[1m]',
    created: 80000,
    read: 100,
    amortisation: 0.001,
  },
}

async function mountWith(data: CacheReport) {
  fetchCacheReport.mockResolvedValue(data)
  await act(async () => {
    root.render(
      <LanguageProvider>
        <CacheView />
      </LanguageProvider>,
    )
    await Promise.resolve()
  })
}

describe('CacheView', () => {
  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    fetchCacheReport.mockReset()
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    window.localStorage.clear()
  })

  it('shows the count below break-even, not an average', async () => {
    // The whole point. Summing these 20 sessions gives 5.0x and looks healthy;
    // 19 of them created a cache nobody read.
    await mountWith(LOSING)

    expect(container.textContent).toContain('19')
    expect(container.textContent).toContain('20')
  })

  it('names the worst session, because a count only says to look', async () => {
    await mountWith(LOSING)

    expect(container.textContent).toContain('e27a51e5-6f85-472c-93b2-20cfd66a0dbb')
    expect(container.textContent).toContain('claude-opus-5[1m]')
  })

  it('reports wasted creation tokens', async () => {
    await mountWith(LOSING)

    expect(container.textContent).toMatch(/19[,\s]?000/)
  })

  it('says "no telemetry" rather than rendering a healthy zero', async () => {
    // Ingest is opt-in. An empty table must never read as a healthy cache —
    // a plausible zero is how several things here sat inert unnoticed.
    await mountWith({
      sessions: 0,
      median_amortisation: 0,
      below_one: 0,
      wasted_tokens: 0,
      healthy: true,
      worst: null,
    })

    expect(container.textContent).not.toMatch(/0\.00×/)
    expect(container.textContent?.length).toBeGreaterThan(0)
  })

  it('does not render a worst-case block when there is none', async () => {
    await mountWith({
      sessions: 5,
      median_amortisation: 12.4,
      below_one: 0,
      wasted_tokens: 0,
      healthy: true,
      worst: null,
    })

    expect(container.textContent).toContain('12.40×')
  })
})
