import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { AnalyticsCost, AnalyticsProviders } from '@/lib/mirador-api'

const mocks = vi.hoisted(() => ({
  fetchAnalyticsCost: vi.fn(),
  fetchAnalyticsProviders: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, ...mocks }
})

import { CostView } from './CostView'

let container: HTMLDivElement
let root: Root

const cost: AnalyticsCost = {
  overall: { total_steps: 20, input_tokens: 10000, output_tokens: 4000, cost_usd: 1.234, unpriced_steps: 2 },
  by_provider: [
    { provider: 'anthropic', total_steps: 15, input_tokens: 8000, output_tokens: 3000, cost_usd: 1.0, unpriced_steps: 1 },
  ],
  by_model: [
    { model: 'claude-sonnet-5', total_steps: 15, input_tokens: 8000, output_tokens: 3000, cost_usd: 1.0, unpriced_steps: 1 },
  ],
}

const providers: AnalyticsProviders = {
  by_provider: [
    {
      provider: 'anthropic',
      total: 15,
      outcomes: { succeeded: 13, failed: 1, skipped: 0, other: 1 },
      outcome_rates: { succeeded: 0.8667, failed: 0.0667, skipped: 0, other: 0.0667 },
    },
  ],
  by_model: [
    {
      model: 'claude-sonnet-5',
      total: 15,
      outcomes: { succeeded: 13, failed: 1, skipped: 0, other: 1 },
      outcome_rates: { succeeded: 0.8667, failed: 0.0667, skipped: 0, other: 0.0667 },
    },
  ],
}

function mount() {
  act(() => {
    root.render(<CostView />)
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

describe('CostView', () => {
  it('shows loading indicators before data resolves', () => {
    for (const mock of Object.values(mocks)) mock.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelectorAll('[role="status"]').length).toBeGreaterThan(0)
  })

  it('renders total cost, tokens, unpriced coverage, and per-provider/model breakdowns', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)
    mocks.fetchAnalyticsProviders.mockResolvedValue(providers)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('$1.234')
    expect(container.textContent).toMatch(/unpriced steps/i)
    expect(container.querySelector('[data-tone="warning"]')?.textContent).toContain('2')
    expect(container.textContent).toContain('anthropic')
    expect(container.textContent).toContain('claude-sonnet-5')
    expect(container.textContent).toContain('10,000')
  })

  it('shows an empty state when there are no steps yet', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue({
      overall: { total_steps: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, unpriced_steps: 0 },
      by_provider: [],
      by_model: [],
    } satisfies AnalyticsCost)
    mocks.fetchAnalyticsProviders.mockResolvedValue({ by_provider: [], by_model: [] } satisfies AnalyticsProviders)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no cost data yet/i)
  })

  it('shows an error card when an endpoint rejects', async () => {
    mocks.fetchAnalyticsCost.mockRejectedValue(new Error('cost endpoint down'))
    mocks.fetchAnalyticsProviders.mockResolvedValue(providers)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('cost endpoint down')
  })

  it('renders French card titles and StatCard labels when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)
    mocks.fetchAnalyticsProviders.mockResolvedValue(providers)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <CostView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Coûts et tokens')
    expect(container.textContent).toContain('Coût total')
    expect(container.textContent).toContain("Tokens d'entrée")
  })
})
