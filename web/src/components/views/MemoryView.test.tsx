import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
// Raw source text of the component under test (Vite's `?raw` import — see
// `vite/client.d.ts`), used ONLY to assert it never opts out of React's
// auto-escaping. Same convention as `MemoryQualityView.test.tsx`.
import memoryViewSource from './MemoryView.tsx?raw'
import { ApiForbiddenError } from '@/lib/api'
import type {
  MemoriesResponse,
  MemoryEvaluationsResponse,
  MemoryGapsResponse,
  MemoryGrowth,
  MemoryJournalResponse,
  MemoryReality,
} from '@/lib/pollen-api'

const mocks = vi.hoisted(() => ({
  fetchMemoryReality: vi.fn(),
  fetchMemoryGaps: vi.fn(),
  fetchMemoryEvaluations: vi.fn(),
  fetchMemoryJournal: vi.fn(),
  fetchMemoryGrowth: vi.fn(),
  fetchMemories: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, ...mocks }
})

import { MemoryView } from './MemoryView'

let container: HTMLDivElement
let root: Root

const emptyReality: MemoryReality = {
  search_success_rate: 0,
  total_searches: 0,
  no_result_count: 0,
  avg_freshness_seconds: 0,
  declared_reliability: 0,
  total_evaluations: 0,
}

const filledReality: MemoryReality = {
  search_success_rate: 0.82,
  total_searches: 40,
  no_result_count: 7,
  avg_freshness_seconds: 196200,
  declared_reliability: 0.9,
  total_evaluations: 10,
}

const emptyGrowth: MemoryGrowth = {
  total: 0,
  memories_by_namespace: [],
  growth_series: [],
  authorship: null,
  by_actor: [],
  source: 'mem0',
}

const filledGrowth: MemoryGrowth = {
  total: 128,
  memories_by_namespace: [
    { namespace: 'runbooks', count: 80 },
    { namespace: 'incidents', count: 48 },
  ],
  growth_series: [
    { date: '2026-07-18', created: 4 },
    { date: '2026-07-19', created: 9 },
    { date: '2026-07-20', created: 3 },
  ],
  authorship: null,
  by_actor: [
    { actor: 'claude', count: 100 },
    { actor: 'alice', count: 28 },
  ],
  source: 'mem0',
}

/** Every endpoint MemoryView's three tabs (across all of Quality/Growth/
 * Search) could call, defaulted to a genuinely-empty-but-successful
 * response — individual tests override just the ones they care about. */
function mockAllEmpty() {
  mocks.fetchMemoryReality.mockResolvedValue(emptyReality)
  mocks.fetchMemoryGaps.mockResolvedValue({ gaps: [] } satisfies MemoryGapsResponse)
  mocks.fetchMemoryEvaluations.mockResolvedValue({ evaluations: [] } satisfies MemoryEvaluationsResponse)
  mocks.fetchMemoryJournal.mockResolvedValue({ journal: [] } satisfies MemoryJournalResponse)
  mocks.fetchMemoryGrowth.mockResolvedValue(emptyGrowth)
  mocks.fetchMemories.mockResolvedValue({ configured: true, memories: [] } satisfies MemoriesResponse)
}

function mount() {
  act(() => {
    root.render(<MemoryView />)
  })
}

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

async function clickTab(label: string) {
  const tab = Array.from(container.querySelectorAll('[role="tab"]')).find(
    (el) => el.textContent === label,
  ) as HTMLElement
  await act(async () => {
    click(tab)
    await Promise.resolve()
    await Promise.resolve()
  })
}

beforeEach(() => {
  mockAllEmpty()
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

describe('MemoryView — tabs', () => {
  it('renders exactly three tabs: Quality, Growth, Search', async () => {
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(['Quality', 'Growth', 'Search'])
  })

  it('defaults to the Quality tab, rendering MemoryQualityView content', async () => {
    mocks.fetchMemoryReality.mockResolvedValue(filledReality)
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const qualityTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Quality',
    )
    expect(qualityTab?.getAttribute('aria-selected')).toBe('true')
    expect(container.textContent).toContain('82%')
    expect(mocks.fetchMemoryReality).toHaveBeenCalled()
    // Growth/Search haven't been fetched yet — only the active tab mounts.
    expect(mocks.fetchMemoryGrowth).not.toHaveBeenCalled()
    expect(mocks.fetchMemories).not.toHaveBeenCalled()
  })

  it('switching to Search shows the existing Mem0View search box', async () => {
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    await clickTab('Search')

    expect(container.textContent).toContain('Mem0 memory search')
    expect(container.querySelector('input[aria-label="Search memories"]')).not.toBeNull()
  })

  it('switching to Growth fetches and renders /v1/memory/growth data', async () => {
    mocks.fetchMemoryGrowth.mockResolvedValue(filledGrowth)
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    await clickTab('Growth')

    expect(mocks.fetchMemoryGrowth).toHaveBeenCalled()
    expect(container.textContent).toContain('128')
    expect(container.textContent).toContain('runbooks')
    expect(container.textContent).toContain('incidents')
    expect(container.textContent).toContain('claude')
    expect(container.textContent).toContain('alice')
    expect(container.querySelector('[data-slot="distribution-bar"]')).not.toBeNull()
    expect(container.querySelector('[data-slot="sparkline"]')).not.toBeNull()
  })
})

describe('MemoryView — Growth tab honesty', () => {
  it('never fabricates an authorship human/agent split — always renders "not available"', async () => {
    mocks.fetchMemoryGrowth.mockResolvedValue(filledGrowth)
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })
    await clickTab('Growth')

    const note = container.querySelector('[data-testid="memory-authorship-note"]')
    expect(note).not.toBeNull()
    expect(note?.textContent).toMatch(/not available/i)
  })

  it('renders one honest empty state when growth is genuinely empty (no fabricated zeros)', async () => {
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })
    await clickTab('Growth')

    expect(container.querySelector('[data-slot="metric-readout-value"]')).toBeNull()
    // Empty means "what would fill this", not "nothing".
    const empty = container.querySelector('[data-testid="memory-growth-empty"]')
    expect(empty).not.toBeNull()
    expect(empty?.textContent).toMatch(/nothing stored in this window/i)
    expect(empty?.textContent).toMatch(/as agents store memories/i)
  })

  it('shows a graceful "requires token" message for a 403 on /v1/memory/growth', async () => {
    mocks.fetchMemoryGrowth.mockRejectedValue(new ApiForbiddenError())
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })
    await clickTab('Growth')

    expect(container.querySelector('[data-testid="memory-growth-forbidden"]')).not.toBeNull()
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('shows a generic error card (not the forbidden message) for a non-403 growth failure', async () => {
    mocks.fetchMemoryGrowth.mockRejectedValue(new Error('network down'))
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })
    await clickTab('Growth')

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toContain('network down')
    expect(container.querySelector('[data-testid="memory-growth-forbidden"]')).toBeNull()
  })
})

describe('MemoryView — security', () => {
  it('CRITICAL — XSS safety: renders untrusted namespace/actor strings as literal text, never markup', async () => {
    mocks.fetchMemoryGrowth.mockResolvedValue({
      ...filledGrowth,
      memories_by_namespace: [{ namespace: '<img src=x onerror=alert(1)>', count: 3 }],
      by_actor: [{ actor: '<script>alert(2)</script>', count: 3 }],
    } satisfies MemoryGrowth)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })
    await clickTab('Growth')

    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(container.textContent).toContain('<script>alert(2)</script>')
  })

  it('never uses dangerouslySetInnerHTML anywhere in the component source', () => {
    expect(memoryViewSource).not.toContain('dangerouslySetInnerHTML')
  })

  // Regression: the app shell mounts a VERTICAL Tabs root (sidebar nav) and
  // this view mounts its own horizontal Tabs inside it. They used to collide
  // over a shared Tailwind `group/tabs` name, which rendered this tab bar as
  // a vertical stack in a tiny box.
  it('CRITICAL: the tab bar stays horizontal when nested inside a vertical Tabs root', async () => {
    const { Tabs, TabsContent } = await import('@/components/ui/tabs')
    await act(async () => {
      root.render(
        <Tabs orientation="vertical" defaultValue="memory">
          <TabsContent value="memory">
            <MemoryView />
          </TabsContent>
        </Tabs>,
      )
      await Promise.resolve()
    })

    const list = container.querySelector('[data-testid="memory-tabs"]')
    expect(list).not.toBeNull()
    expect(list?.getAttribute('data-orientation')).toBe('horizontal')
    // No UNCONDITIONAL flex-col, and no ancestor-scoped orientation variant
    // (only the self-scoped `data-vertical:` one, which cannot match here).
    expect(list?.className).not.toMatch(/(^|\s)flex-col(\s|$)/)
    expect(list?.className).not.toMatch(/group-data-vertical\/tabs/)
    expect(list?.className).toMatch(/data-vertical:flex-col/)
  })
})
