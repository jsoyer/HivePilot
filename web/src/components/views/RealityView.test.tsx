import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
// Raw source text of the component under test (Vite's `?raw` import — see
// `vite/client.d.ts`), used ONLY to assert it never opts out of React's
// auto-escaping. Avoids `node:fs` so this test stays consistent with the
// rest of `src/`, which targets the browser (`tsconfig.app.json` has no
// `"node"` types).
import realityViewSource from './RealityView.tsx?raw'
import { ApiForbiddenError } from '@/lib/api'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type {
  MemoryEvaluationsResponse,
  MemoryGapsResponse,
  MemoryJournalResponse,
  MemoryReality,
} from '@/lib/mirador-api'

const mocks = vi.hoisted(() => ({
  fetchMemoryReality: vi.fn(),
  fetchMemoryGaps: vi.fn(),
  fetchMemoryEvaluations: vi.fn(),
  fetchMemoryJournal: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, ...mocks }
})

import { RealityView } from './RealityView'

let container: HTMLDivElement
let root: Root

const reality: MemoryReality = {
  search_success_rate: 0.82,
  total_searches: 40,
  no_result_count: 7,
  avg_freshness_seconds: 196200, // 2 days, 6 hours, 30 minutes -> "2d 6h"
  declared_reliability: 0.9,
  total_evaluations: 10,
}

const emptyReality: MemoryReality = {
  search_success_rate: 0,
  total_searches: 0,
  no_result_count: 0,
  avg_freshness_seconds: 0,
  declared_reliability: 0,
  total_evaluations: 0,
}

const gaps: MemoryGapsResponse = {
  gaps: [
    { namespace: 'runbooks', no_result_count: 5, top_queries: ['deploy failure', 'rollback steps'] },
    { namespace: 'incidents', no_result_count: 2, top_queries: ['db outage'] },
  ],
}

const evaluations: MemoryEvaluationsResponse = {
  evaluations: [
    {
      ts: '2026-07-20T10:00:00Z',
      namespace: 'runbooks',
      ref_key: 'r1',
      useful: true,
      note: 'Spot on',
      actor: 'alice',
    },
    {
      ts: '2026-07-19T10:00:00Z',
      namespace: 'incidents',
      ref_key: null,
      useful: false,
      note: null,
      actor: 'bob',
    },
  ],
}

const journal: MemoryJournalResponse = {
  journal: [
    {
      ts: '2026-07-20T11:00:00Z',
      op: 'search',
      namespace: 'runbooks',
      query_or_key: 'deploy failure',
      result_count: 3,
      found: null,
      freshness_seconds: 0,
      actor: 'claude',
    },
    {
      ts: '2026-07-20T10:00:00Z',
      op: 'read',
      namespace: 'incidents',
      query_or_key: 'incident-42',
      result_count: null,
      found: true,
      freshness_seconds: null,
      actor: 'claude',
    },
  ],
}

function mockAllSuccess() {
  mocks.fetchMemoryReality.mockResolvedValue(reality)
  mocks.fetchMemoryGaps.mockResolvedValue(gaps)
  mocks.fetchMemoryEvaluations.mockResolvedValue(evaluations)
  mocks.fetchMemoryJournal.mockResolvedValue(journal)
}

function mount() {
  act(() => {
    root.render(<RealityView />)
  })
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset()
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

describe('RealityView', () => {
  it('shows loading indicators before any endpoint resolves', () => {
    for (const mock of Object.values(mocks)) mock.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelectorAll('[role="status"]').length).toBeGreaterThan(0)
  })

  it('renders KPI StatCards from the reality summary', async () => {
    mockAllSuccess()
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('82%')
    expect(container.textContent).toContain('90%')
    expect(container.textContent).toContain('2d 6h')
    expect(container.textContent).toContain('40')
    expect(container.textContent).toContain('10')
    // no-result count is a real, always-present count (7) — not fabricated.
    expect(container.textContent).toContain('7')
  })

  it('shows "no data" instead of a fabricated 0% when a rate has a zero denominator (mixed state)', async () => {
    mocks.fetchMemoryReality.mockResolvedValue({
      ...emptyReality,
      total_searches: 40,
      search_success_rate: 0.82,
      // total_evaluations stays 0 — nobody has rated anything yet, which is
      // NOT the same claim as "0% of memories are useful".
    } satisfies MemoryReality)
    mocks.fetchMemoryGaps.mockResolvedValue(gaps)
    mocks.fetchMemoryEvaluations.mockResolvedValue({ evaluations: [] } satisfies MemoryEvaluationsResponse)
    mocks.fetchMemoryJournal.mockResolvedValue(journal)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('82%')
    expect(container.textContent).not.toContain('0%')
  })

  it('renders the honest empty state (no fabricated numbers) when every endpoint resolves empty', async () => {
    mocks.fetchMemoryReality.mockResolvedValue(emptyReality)
    mocks.fetchMemoryGaps.mockResolvedValue({ gaps: [] } satisfies MemoryGapsResponse)
    mocks.fetchMemoryEvaluations.mockResolvedValue({ evaluations: [] } satisfies MemoryEvaluationsResponse)
    mocks.fetchMemoryJournal.mockResolvedValue({ journal: [] } satisfies MemoryJournalResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const empty = container.querySelector('[data-testid="reality-empty-state"]')
    expect(empty).not.toBeNull()
    expect(empty?.textContent).toMatch(/no memory activity recorded yet/i)
    expect(container.querySelector('[data-slot="stat-card"]')).toBeNull()
    expect(container.textContent).not.toContain('0%')
  })

  it('renders the gaps distribution bar with top queries per namespace', async () => {
    mockAllSuccess()
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-slot="distribution-bar"]')).not.toBeNull()
    expect(container.textContent).toContain('runbooks')
    expect(container.textContent).toContain('deploy failure')
    expect(container.textContent).toContain('rollback steps')
    expect(container.textContent).toContain('incidents')
    expect(container.textContent).toContain('db outage')
  })

  it('renders recent evaluations with useful/not-useful markers', async () => {
    mockAllSuccess()
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Spot on')
    expect(container.textContent).toContain('alice')
    expect(container.textContent).toContain('bob')
  })

  it('renders the activity journal table, most-recent first, with correct result and freshness cells', async () => {
    mockAllSuccess()
    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const table = container.querySelector('table')
    expect(table).not.toBeNull()
    const rows = Array.from(table!.querySelectorAll('tbody tr'))
    expect(rows).toHaveLength(2)

    expect(rows[0]?.textContent).toContain('search')
    expect(rows[0]?.textContent).toContain('deploy failure')
    expect(rows[0]?.textContent).toContain('3')
    expect(rows[0]?.textContent).toContain('0s')

    expect(rows[1]?.textContent).toContain('read')
    expect(rows[1]?.textContent).toContain('incident-42')
    expect(rows[1]?.textContent).toContain('✓')
    expect(rows[1]?.textContent).toContain('—')
  })

  it('CRITICAL — XSS safety: renders untrusted journal/evaluation strings as literal text, never markup', async () => {
    mocks.fetchMemoryReality.mockResolvedValue(reality)
    mocks.fetchMemoryGaps.mockResolvedValue({ gaps: [] } satisfies MemoryGapsResponse)
    mocks.fetchMemoryEvaluations.mockResolvedValue({
      evaluations: [
        {
          ts: '2026-07-20T10:00:00Z',
          namespace: 'runbooks',
          ref_key: null,
          useful: true,
          note: '<script>alert(1)</script>',
          actor: 'alice',
        },
      ],
    } satisfies MemoryEvaluationsResponse)
    mocks.fetchMemoryJournal.mockResolvedValue({
      journal: [
        {
          ts: '2026-07-20T11:00:00Z',
          op: 'search',
          namespace: '[red]ns[/]',
          query_or_key: '[red]x[/]<img src=x onerror=alert(2)>',
          result_count: 0,
          found: null,
          freshness_seconds: 5,
          actor: 'claude',
        },
      ],
    } satisfies MemoryJournalResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<script>alert(1)</script>')
    expect(container.textContent).toContain('[red]x[/]<img src=x onerror=alert(2)>')
    expect(container.textContent).toContain('[red]ns[/]')
  })

  it('never uses dangerouslySetInnerHTML anywhere in the component source', () => {
    expect(realityViewSource).not.toContain('dangerouslySetInnerHTML')
  })

  it('shows the ApiForbidden state for a section whose endpoint 403s, without breaking the others', async () => {
    mocks.fetchMemoryReality.mockResolvedValue(reality)
    mocks.fetchMemoryGaps.mockRejectedValue(new ApiForbiddenError())
    mocks.fetchMemoryEvaluations.mockResolvedValue(evaluations)
    mocks.fetchMemoryJournal.mockResolvedValue(journal)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="reality-forbidden-gaps"]')).not.toBeNull()
    expect(container.querySelector('[role="alert"]')).toBeNull()
    // The other sections still rendered — one 403 doesn't blank the panel.
    expect(container.textContent).toContain('Spot on')
  })

  it('shows a generic error card (not the forbidden message) for a non-403 failure', async () => {
    mocks.fetchMemoryReality.mockResolvedValue(reality)
    mocks.fetchMemoryGaps.mockRejectedValue(new Error('network down'))
    mocks.fetchMemoryEvaluations.mockResolvedValue(evaluations)
    mocks.fetchMemoryJournal.mockResolvedValue(journal)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toContain('network down')
    expect(container.querySelector('[data-testid="reality-forbidden-gaps"]')).toBeNull()
  })

  it('renders French card titles when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mockAllSuccess()

    await act(async () => {
      root.render(
        <LanguageProvider>
          <RealityView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Taux de succès des recherches')
    expect(container.textContent).toContain('Recherches sans résultat')
    expect(container.textContent).toContain('Fraîcheur moyenne des rappels')
    expect(container.textContent).toContain('Fiabilité déclarée')
    expect(container.textContent).toContain('Lacunes par namespace')
    expect(container.textContent).toContain('Évaluations récentes')
    expect(container.textContent).toContain('Journal récent')
  })
})
