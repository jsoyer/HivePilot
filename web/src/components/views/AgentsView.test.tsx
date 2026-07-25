import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { AgentsResponse, LessonsResponse, VerdictsResponse } from '@/lib/mirador-api'

const { fetchAgents, fetchLessons, fetchVerdicts } = vi.hoisted(() => ({
  fetchAgents: vi.fn(),
  fetchLessons: vi.fn(),
  fetchVerdicts: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchAgents, fetchLessons, fetchVerdicts }
})

import { AgentsView } from './AgentsView'

const EMPTY_VERDICTS: VerdictsResponse = { verdicts: [], by_role: {} }
const EMPTY_LESSONS: LessonsResponse = { lessons: [], by_role: {} }

const BASE_RESPONSE: AgentsResponse = {
  agents: [
    {
      name: 'gustave',
      display_name: 'Gustave',
      title: 'Lead Developer',
      attributed: false,
      run_count: 0,
      step_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      cost_usd: 0,
      unpriced_steps: 0,
      success_rate: null,
      last_active: null,
    },
    {
      name: 'reviewer',
      display_name: 'Reviewer',
      title: 'Adversarial Reviewer',
      attributed: true,
      run_count: 12,
      step_count: 40,
      input_tokens: 120_000,
      output_tokens: 45_000,
      cost_usd: 3.456,
      unpriced_steps: 0,
      success_rate: 0.9,
      last_active: '2026-07-24T10:00:00Z',
    },
  ],
  unknown: {
    run_count: 0,
    step_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0,
    unpriced_steps: 0,
    success_rate: null,
    last_active: null,
  },
  note: 'Per-role attribution requires steps.role, added in this sprint.',
}

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<AgentsView />)
  })
}

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

beforeEach(() => {
  fetchAgents.mockReset()
  fetchLessons.mockReset()
  fetchVerdicts.mockReset()
  fetchLessons.mockResolvedValue(EMPTY_LESSONS)
  fetchVerdicts.mockResolvedValue(EMPTY_VERDICTS)
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

describe('AgentsView', () => {
  it('shows a loading indicator before the roster resolves', () => {
    fetchAgents.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('CRITICAL: an unattributed role shows "no activity yet", never a fabricated success rate', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="agent-no-activity-gustave"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="agent-no-success-rate-gustave"]')).toBeNull()
    // No stray "0%" for a role that never ran anything.
    const gustaveCard = container.querySelector('[data-testid="agent-card-gustave"]')
    expect(gustaveCard?.textContent).not.toMatch(/0%/)
  })

  it('renders real numbers for an attributed role', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const reviewerCard = container.querySelector('[data-testid="agent-card-reviewer"]')
    expect(reviewerCard?.textContent).toContain('Reviewer')
    expect(reviewerCard?.textContent).toContain('Adversarial Reviewer')
    expect(reviewerCard?.textContent).toContain('$3.456')
    expect(reviewerCard?.textContent).toContain('12')
    expect(reviewerCard?.textContent).toContain('40')
    expect(reviewerCard?.textContent).toContain('90%')
  })

  it('CRITICAL: a null success_rate never renders as a number, even for an attributed role', async () => {
    fetchAgents.mockResolvedValue({
      ...BASE_RESPONSE,
      agents: [
        {
          ...BASE_RESPONSE.agents[1],
          name: 'skipper',
          display_name: 'Skipper',
          success_rate: null,
        },
      ],
    })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="agent-no-success-rate-skipper"]')).not.toBeNull()
    const card = container.querySelector('[data-testid="agent-card-skipper"]')
    expect(card?.textContent).not.toMatch(/\bnull%|\bNaN%/)
  })

  it('CRITICAL: the unknown bucket renders separately, clearly labelled, with the backend note verbatim', async () => {
    fetchAgents.mockResolvedValue({
      ...BASE_RESPONSE,
      unknown: { ...BASE_RESPONSE.unknown, step_count: 500, cost_usd: 12.5, run_count: 30 },
    })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const bucket = container.querySelector('[data-testid="agents-unknown-bucket"]')
    expect(bucket).not.toBeNull()
    expect(bucket?.textContent).toMatch(/unattributed/i)
    expect(bucket?.textContent).toContain('$12.500')
    expect(container.querySelector('[data-testid="agents-note"]')?.textContent).toBe(BASE_RESPONSE.note)
  })

  it('CRITICAL: an empty unknown bucket shows an honest empty state, not zeros dressed up as data', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="agents-unknown-empty"]')).not.toBeNull()
  })

  it('CRITICAL: a role with a recent non-ACCEPT verdict gets the severity stripe/badge, a nominal role does not', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)
    fetchVerdicts.mockResolvedValue({
      verdicts: [],
      by_role: {
        gustave: { total: 3, decision_counts: { ACCEPT: 3 }, kind_counts: { review: 3 } },
        reviewer: { total: 2, decision_counts: { ACCEPT: 1, unknown: 1 }, kind_counts: { challenge: 2 } },
      },
    })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="agent-nonnominal-reviewer"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="agent-nonnominal-gustave"]')).toBeNull()
  })

  it('CRITICAL: XSS — a malicious display_name renders as literal text, never as markup', async () => {
    const malicious = '<img src=x onerror=alert(1)>'
    fetchAgents.mockResolvedValue({
      ...BASE_RESPONSE,
      agents: [{ ...BASE_RESPONSE.agents[1], name: 'evil', display_name: malicious }],
    })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain(malicious)
    expect(container.querySelector('img')).toBeNull()
  })

  it('CRITICAL: a 403 loading the roster renders an inline alert, never a crash', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchAgents.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"][data-testid="agents-forbidden"]')
    expect(alert).not.toBeNull()
  })

  it('opens a per-role drill-down panel on card click, fetching lessons/verdicts scoped to that role', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)
    fetchLessons.mockResolvedValue({
      lessons: [
        {
          id: 1,
          run_id: 5,
          project: 'acme',
          role: 'reviewer',
          task: 'review',
          source_verdict_id: null,
          source_interaction_id: null,
          text: 'Always check for empty-value fail-opens.',
          score: 0.82,
          confidence: 0.9,
          category: 'security',
          validated: 1,
          use_count: 3,
          created_at: '2026-07-20T00:00:00Z',
        },
      ],
      by_role: {},
    })
    fetchVerdicts.mockResolvedValue({
      verdicts: [
        {
          id: 9,
          run_id: 5,
          project: 'acme',
          task: 'review',
          role: 'reviewer',
          kind: 'review',
          decision: null,
          confidence: null,
          summary: 'adversarial review by security: security: FAIL',
          timestamp: '2026-07-20T00:05:00Z',
        },
      ],
      by_role: {},
    })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const card = container.querySelector('[data-testid="agent-card-reviewer"]') as HTMLElement
    await act(async () => {
      click(card)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchLessons).toHaveBeenCalledWith('reviewer')
    expect(fetchVerdicts).toHaveBeenCalledWith('reviewer')

    const dialog = container.querySelector('[role="dialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog?.textContent).toContain('Always check for empty-value fail-opens.')
    expect(dialog?.textContent).toContain('adversarial review by security: security: FAIL')
  })

  it('CRITICAL: XSS — malicious lesson text and verdict summary render as literal text in the drill-down', async () => {
    const malicious = '<img src=x onerror=alert(1)>'
    fetchAgents.mockResolvedValue(BASE_RESPONSE)
    fetchLessons.mockResolvedValue({
      lessons: [
        {
          id: 1,
          run_id: 5,
          project: 'acme',
          role: 'reviewer',
          task: 'review',
          source_verdict_id: null,
          source_interaction_id: null,
          text: malicious,
          score: null,
          confidence: null,
          category: null,
          validated: 0,
          use_count: 0,
          created_at: null,
        },
      ],
      by_role: {},
    })
    fetchVerdicts.mockResolvedValue({
      verdicts: [
        {
          id: 9,
          run_id: 5,
          project: 'acme',
          task: 'review',
          role: 'reviewer',
          kind: 'review',
          decision: null,
          confidence: null,
          summary: malicious,
          timestamp: null,
        },
      ],
      by_role: {},
    })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const card = container.querySelector('[data-testid="agent-card-reviewer"]') as HTMLElement
    await act(async () => {
      click(card)
      await Promise.resolve()
      await Promise.resolve()
    })

    const dialog = container.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog.textContent).toContain(malicious)
    expect(dialog.querySelector('img')).toBeNull()
  })

  it('CRITICAL: honest empty states for lessons/verdicts in the drill-down when a role has none', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)
    fetchLessons.mockResolvedValue(EMPTY_LESSONS)
    fetchVerdicts.mockResolvedValue(EMPTY_VERDICTS)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const card = container.querySelector('[data-testid="agent-card-reviewer"]') as HTMLElement
    await act(async () => {
      click(card)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no lessons recorded/i)
    expect(container.textContent).toMatch(/no verdicts recorded/i)
  })

  it('closes the drill-down panel via the close button', async () => {
    fetchAgents.mockResolvedValue(BASE_RESPONSE)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const card = container.querySelector('[data-testid="agent-card-reviewer"]') as HTMLElement
    await act(async () => {
      click(card)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="dialog"]')).not.toBeNull()

    const closeButton = container.querySelector('[aria-label="Close agent detail"]') as HTMLElement
    await act(async () => {
      click(closeButton)
      await Promise.resolve()
    })
    expect(container.querySelector('[role="dialog"]')).toBeNull()
  })

  it('renders French labels when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchAgents.mockResolvedValue(BASE_RESPONSE)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <AgentsView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Agents')
    expect(container.textContent).toMatch(/aucune activité attribuée/i)
  })
})
