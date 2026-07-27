import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { ModelsSummary } from '@/lib/pollen-api'

const mocks = vi.hoisted(() => ({
  fetchModels: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, ...mocks }
})

import { ModelsView } from './ModelsView'

let container: HTMLDivElement
let root: Root

const models: ModelsSummary = {
  models: [
    {
      model: 'claude-sonnet-5',
      step_count: 12,
      input_tokens: 8000,
      output_tokens: 3000,
      cost_usd: 1.5,
      unpriced_steps: 0,
      success_rate: 0.9167,
      share_of_spend: 0.75,
    },
    {
      model: 'claude-haiku',
      step_count: 4,
      input_tokens: 1000,
      output_tokens: 400,
      cost_usd: 0.5,
      unpriced_steps: 1,
      success_rate: null,
      share_of_spend: 0.25,
    },
  ],
  overall: {
    total_steps: 16,
    input_tokens: 9000,
    output_tokens: 3400,
    cost_usd: 2.0,
    unpriced_steps: 1,
    succeeded_runs: 10,
    cost_per_successful_run: 0.2,
  },
  latency_available: false,
  latency_note: 'p50/p95 latency is not computable from current data.',
}

function mount() {
  act(() => {
    root.render(<ModelsView />)
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

describe('ModelsView', () => {
  it('shows a loading indicator before data resolves', () => {
    mocks.fetchModels.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelectorAll('[role="status"]').length).toBeGreaterThan(0)
  })

  it('renders the cost-per-successful-run hero and per-model rows', async () => {
    mocks.fetchModels.mockResolvedValue(models)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('$0.200')
    expect(container.textContent).toContain('claude-sonnet-5')
    expect(container.textContent).toContain('claude-haiku')
    expect(container.textContent).toContain('$1.500')
    expect(container.textContent).toContain('8,000')
    expect(container.textContent).toContain('92%')
    expect(container.textContent).toMatch(/no attempts/i)
    // step_count column (required by the sprint's per-model table contract)
    expect(container.textContent).toContain('12')
    expect(container.textContent).toContain('4')
  })

  it('honestly shows latency as not available (never fabricates p50/p95)', async () => {
    mocks.fetchModels.mockResolvedValue(models)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/not available/i)
    expect(container.textContent).not.toMatch(/p50.*ms|p95.*ms/i)
  })

  it('shows the no-succeeded-runs honest state when cost_per_successful_run is null', async () => {
    mocks.fetchModels.mockResolvedValue({
      ...models,
      overall: { ...models.overall, succeeded_runs: 0, cost_per_successful_run: null },
    } satisfies ModelsSummary)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no succeeded runs/i)
  })

  it('shows an honest empty state when there are no models yet', async () => {
    mocks.fetchModels.mockResolvedValue({
      models: [],
      overall: {
        total_steps: 0,
        input_tokens: 0,
        output_tokens: 0,
        cost_usd: 0,
        unpriced_steps: 0,
        succeeded_runs: 0,
        cost_per_successful_run: null,
      },
      latency_available: false,
      latency_note: 'p50/p95 latency is not computable from current data.',
    } satisfies ModelsSummary)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no model data yet/i)
  })

  it('renders an ApiForbiddenError as an error card instead of crashing', async () => {
    mocks.fetchModels.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).not.toBeNull()
  })

  it('renders untrusted model names as plain text (XSS-safe)', async () => {
    mocks.fetchModels.mockResolvedValue({
      ...models,
      models: [
        {
          model: '<script>window.__xss = true</script>',
          step_count: 1,
          input_tokens: 10,
          output_tokens: 5,
          cost_usd: 0.01,
          unpriced_steps: 0,
          success_rate: 1,
          share_of_spend: 1,
        },
      ],
    } satisfies ModelsSummary)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('script')).toBeNull()
    expect(container.textContent).toContain('<script>window.__xss = true</script>')
    expect((window as unknown as { __xss?: boolean }).__xss).toBeUndefined()
  })

  it('renders French labels when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mocks.fetchModels.mockResolvedValue(models)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <ModelsView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Coût par run réussi')
  })
})
