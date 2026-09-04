import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AnalyticsCost, ProviderFallback } from '@/lib/pollen-api'

const { fetchAnalyticsCost, fetchProviderFallbacks, fetchOnboardingMachine, verifyModel } =
  vi.hoisted(() => ({
    fetchAnalyticsCost: vi.fn(),
    fetchProviderFallbacks: vi.fn(),
    fetchOnboardingMachine: vi.fn(),
    verifyModel: vi.fn(),
  }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return {
    ...actual,
    fetchAnalyticsCost,
    fetchProviderFallbacks,
    fetchOnboardingMachine,
    verifyModel,
  }
})

import { ProvidersView } from './ProvidersView'

function accum(overrides: Record<string, number>) {
  return {
    total_steps: 0,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0,
    unpriced_steps: 0,
    ...overrides,
  }
}

function cost(providers: { provider: string; cost_usd: number; tokens: number }[]): AnalyticsCost {
  return {
    overall: accum({ cost_usd: providers.reduce((s, p) => s + p.cost_usd, 0) }),
    by_provider: providers.map((p) => ({
      provider: p.provider,
      ...accum({ cost_usd: p.cost_usd, input_tokens: p.tokens }),
    })),
    by_model: [],
    by_project: [],
    by_role: null,
    by_role_note: '',
    unpriced_models: [],
  }
}

function fallback(overrides: Partial<ProviderFallback>): ProviderFallback {
  return {
    provider: 'claude',
    count: 1,
    last_at: '2026-08-31T10:00:00Z',
    last_reason: 'quota',
    last_to: 'codex',
    ...overrides,
  }
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchAnalyticsCost.mockReset()
  fetchProviderFallbacks.mockReset()
  fetchOnboardingMachine.mockReset()
  verifyModel.mockReset()
  fetchProviderFallbacks.mockResolvedValue({ hours: 24, providers: [] })
  fetchOnboardingMachine.mockResolvedValue({
    local: [
      {
        kind: 'ollama',
        base_url: 'http://127.0.0.1:11434/v1',
        reachable: true,
        models: ['llama3.2'],
        error: null,
      },
    ],
    cli: [{ kind: 'claude', state: 'present', login_available: true }],
  })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.restoreAllMocks()
})

async function mountResolved() {
  await act(async () => {
    root.render(<ProvidersView />)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('ProvidersView', () => {
  it('renders a spend row per provider', async () => {
    fetchAnalyticsCost.mockResolvedValue(
      cost([
        { provider: 'claude', cost_usd: 1.25, tokens: 1000 },
        { provider: 'grok', cost_usd: 0.5, tokens: 500 },
      ]),
    )
    await mountResolved()

    expect(container.querySelector('[data-testid="providers-row-claude"]')?.textContent).toContain(
      '$1.250',
    )
    expect(container.querySelector('[data-testid="providers-row-grok"]')).not.toBeNull()
  })

  it('shows a fallback badge on a provider that fell over', async () => {
    fetchAnalyticsCost.mockResolvedValue(cost([{ provider: 'claude', cost_usd: 2, tokens: 10 }]))
    fetchProviderFallbacks.mockResolvedValue({
      hours: 24,
      providers: [fallback({ provider: 'claude', count: 3, last_reason: 'quota' })],
    })
    await mountResolved()

    const badge = container.querySelector('[data-testid="providers-fallback-claude"]')
    expect(badge).not.toBeNull()
    expect(badge?.textContent).toContain('3')
    expect(badge?.textContent?.toLowerCase()).toContain('quota')
  })

  it('surfaces a provider that fell over even with no recorded spend', async () => {
    fetchAnalyticsCost.mockResolvedValue(cost([{ provider: 'claude', cost_usd: 2, tokens: 10 }]))
    fetchProviderFallbacks.mockResolvedValue({
      hours: 24,
      providers: [fallback({ provider: 'cursor', count: 1, last_reason: 'unavailable' })],
    })
    await mountResolved()

    expect(container.querySelector('[data-testid="providers-row-cursor"]')).not.toBeNull()
    expect(
      container.querySelector('[data-testid="providers-fallback-cursor"]')?.textContent?.toLowerCase(),
    ).toContain('unavailable')
  })

  it('shows an empty state when nothing is recorded', async () => {
    fetchAnalyticsCost.mockResolvedValue(cost([]))
    await mountResolved()

    expect(container.querySelector('[data-testid="providers-table"]')).toBeNull()
    expect(container.textContent).toMatch(/No provider activity|Aucune activité/i)
  })

  it('lists a reachable local model and a CLI session already on the machine', async () => {
    fetchAnalyticsCost.mockResolvedValue(cost([]))
    await mountResolved()

    expect(container.querySelector('[data-testid="local-backend-ollama"]')?.textContent).toContain(
      'llama3.2',
    )
    expect(container.querySelector('[data-testid="cli-session-claude"]')?.textContent).toMatch(
      /signed in|connecté/i,
    )
  })
})
