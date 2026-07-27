import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { AnalyticsCost } from '@/lib/pollen-api'

const mocks = vi.hoisted(() => ({
  fetchAnalyticsCost: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
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
    { model: 'claude-haiku', total_steps: 5, input_tokens: 2000, output_tokens: 1000, cost_usd: 0.234, unpriced_steps: 1 },
  ],
  by_project: [
    { project: 'acme-web', total_steps: 15, input_tokens: 8000, output_tokens: 3000, cost_usd: 1.0, unpriced_steps: 0 },
    { project: 'acme-cli', total_steps: 5, input_tokens: 2000, output_tokens: 1000, cost_usd: 0.234, unpriced_steps: 2 },
  ],
  by_role: null,
  by_role_note: 'by_role is unavailable',
  unpriced_models: ['claude-sonnet-5'],
}

const emptyCost: AnalyticsCost = {
  overall: { total_steps: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, unpriced_steps: 0 },
  by_provider: [],
  by_model: [],
  by_project: [],
  by_role: null,
  by_role_note: 'by_role is unavailable',
  unpriced_models: [],
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
  it('shows a loading indicator before data resolves', () => {
    mocks.fetchAnalyticsCost.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelectorAll('[role="status"]').length).toBeGreaterThan(0)
  })

  it('renders total spend, by-model, and by-project breakdowns from real data', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('$1.234')
    expect(container.textContent).toContain('claude-sonnet-5')
    expect(container.textContent).toContain('claude-haiku')
    expect(container.textContent).toContain('acme-web')
    expect(container.textContent).toContain('acme-cli')
    expect(container.textContent).toContain('10,000')
    expect(mocks.fetchAnalyticsCost).toHaveBeenCalledWith(30)
  })

  it('never renders a budget/burn gauge or fake ceiling (no daily-budget field exists)', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-slot="burn-ribbon"]')).toBeNull()
    expect(container.querySelector('[data-slot="gauge"]')).toBeNull()
    expect(container.textContent).not.toMatch(/budget/i)
  })

  it('shows the unpriced-models banner only when unpriced_models is non-empty', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const banner = container.querySelector('[data-testid="cost-unpriced-banner"]')
    expect(banner).not.toBeNull()
    expect(banner?.textContent).toContain('claude-sonnet-5')
  })

  it('hides the unpriced-models banner when unpriced_models is empty', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue({ ...cost, unpriced_models: [] } satisfies AnalyticsCost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="cost-unpriced-banner"]')).toBeNull()
  })

  it('shows an honest empty state (real zeros) when there is no cost data yet', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue(emptyCost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no cost data yet/i)
    expect(container.querySelector('[data-testid="cost-unpriced-banner"]')).toBeNull()
  })

  it('switches the fetch window when a different day option is clicked', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(mocks.fetchAnalyticsCost).toHaveBeenLastCalledWith(30)

    const sevenDayButton = container.querySelector('[data-testid="cost-window-7"]') as HTMLButtonElement
    expect(sevenDayButton).not.toBeNull()

    await act(async () => {
      sevenDayButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(mocks.fetchAnalyticsCost).toHaveBeenLastCalledWith(7)
  })

  it('renders an ApiForbiddenError as an error card instead of crashing', async () => {
    mocks.fetchAnalyticsCost.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).not.toBeNull()
  })

  it('renders untrusted model/project names as plain text (XSS-safe)', async () => {
    mocks.fetchAnalyticsCost.mockResolvedValue({
      ...cost,
      by_model: [
        {
          model: '<img src=x onerror=alert(1)>',
          total_steps: 1,
          input_tokens: 1,
          output_tokens: 1,
          cost_usd: 0.01,
          unpriced_steps: 0,
        },
      ],
      by_project: [],
      unpriced_models: [],
    } satisfies AnalyticsCost)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('renders French card titles when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mocks.fetchAnalyticsCost.mockResolvedValue(cost)

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
  })
})
