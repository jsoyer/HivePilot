import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRoster, AgentsResponse, VerdictsResponse } from '@/lib/pollen-api'

const { fetchAgents, fetchLessons, fetchVerdicts } = vi.hoisted(() => ({
  fetchAgents: vi.fn(),
  fetchLessons: vi.fn(),
  fetchVerdicts: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchAgents, fetchLessons, fetchVerdicts }
})

import { AgentsView } from './AgentsView'
import agentsViewSource from './AgentsView.tsx?raw'

let container: HTMLDivElement
let root: Root

function agent(overrides: Partial<AgentRoster>): AgentRoster {
  return {
    name: 'developer',
    display_name: 'Developer',
    title: 'writes code',
    attributed: true,
    run_count: 4,
    step_count: 9,
    input_tokens: 1000,
    output_tokens: 500,
    cost_usd: 1,
    unpriced_steps: 0,
    success_rate: 1,
    last_active: '2026-07-18T10:00:00Z',
    ...overrides,
  }
}

function response(agents: AgentRoster[]): AgentsResponse {
  return {
    agents,
    unknown: {
      run_count: 0,
      step_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      cost_usd: 0,
      unpriced_steps: 0,
      success_rate: null,
      last_active: null,
      note: 'ENGINEERING NOTE ABOUT steps.role AND _LATENCY_UNAVAILABLE_NOTE',
    },
    note: 'Per-role attribution requires steps.role, added in the Pollen Agent Panels backend sprint.',
  } as AgentsResponse
}

const noVerdicts: VerdictsResponse = { verdicts: [], by_role: {} }

async function mount() {
  await act(async () => {
    root.render(<AgentsView />)
    await Promise.resolve()
    await Promise.resolve()
  })
}

beforeEach(() => {
  fetchAgents.mockReset()
  fetchLessons.mockReset()
  fetchVerdicts.mockReset()
  fetchLessons.mockResolvedValue({ lessons: [], by_role: {} })
  fetchVerdicts.mockResolvedValue(noVerdicts)
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
  it('CRITICAL: never renders the backend engineering note in the operator UI', async () => {
    fetchAgents.mockResolvedValue(response([agent({})]))
    await mount()

    expect(container.textContent).not.toContain('steps.role')
    expect(container.textContent).not.toContain('_LATENCY_UNAVAILABLE_NOTE')
    expect(container.textContent).not.toMatch(/backend sprint/i)
  })

  it('CRITICAL: surfaces a low success rate at the top of the page, not buried in the roster', async () => {
    fetchAgents.mockResolvedValue(
      response([
        agent({ name: 'a', display_name: 'A', success_rate: 1 }),
        agent({ name: 'b', display_name: 'B', success_rate: 1 }),
        agent({ name: 'weak', display_name: 'Weak', success_rate: 0.2 }),
        agent({ name: 'c', display_name: 'C', success_rate: 1 }),
      ]),
    )
    await mount()

    const band = container.querySelector('[data-testid="agents-attention-band"]')
    expect(band).not.toBeNull()
    expect(band?.textContent).toContain('Weak')
    expect(band?.textContent).toMatch(/20%/)

    // And it sorts first in the roster too.
    expect(container.querySelector('tbody tr')?.getAttribute('data-testid')).toBe('agent-row-weak')
  })

  it('CRITICAL: a low success rate is visually distinguishable from a healthy one', async () => {
    fetchAgents.mockResolvedValue(
      response([agent({ name: 'good', success_rate: 1 }), agent({ name: 'weak', success_rate: 0.2 })]),
    )
    await mount()

    const weak = container.querySelector('[data-testid="agent-success-rate-weak"]')
    const good = container.querySelector('[data-testid="agent-success-rate-good"]')
    expect(weak?.className).toMatch(/color-crit/)
    expect(weak?.className).toMatch(/font-semibold/)
    expect(good?.className).toMatch(/color-good/)
    expect(good?.className).not.toMatch(/font-semibold/)
    // The whole row carries a severity stripe, not just the number.
    expect(container.querySelector('[data-testid="agent-row-weak"]')?.className).toMatch(
      /border-l-\[var\(--color-crit\)\]/,
    )
    expect(container.querySelector('[data-testid="agent-row-good"]')?.className).not.toMatch(
      /border-l-\[var\(--color-(crit|warn)\)\]/,
    )
  })

  it('CRITICAL: cost magnitude is encoded as weight and a proportional bar, not text alone', async () => {
    fetchAgents.mockResolvedValue(
      response([agent({ name: 'big', cost_usd: 7.259 }), agent({ name: 'small', cost_usd: 0 })]),
    )
    await mount()

    const big = container.querySelector('[data-testid="agent-cost-big"]')
    const small = container.querySelector('[data-testid="agent-cost-small"]')
    expect(big?.textContent).toBe('$7.259')
    expect(small?.textContent).toBe('$0.000')
    expect(big?.className).toMatch(/font-semibold/)
    expect(small?.className).not.toMatch(/font-semibold/)

    expect((container.querySelector('[data-testid="agent-cost-bar-big"]') as HTMLElement).style.width).toBe(
      '100%',
    )
    expect(
      (container.querySelector('[data-testid="agent-cost-bar-small"]') as HTMLElement).style.width,
    ).toBe('0%')
  })

  it('sorts by cost descending once severity is equal', async () => {
    fetchAgents.mockResolvedValue(
      response([
        agent({ name: 'cheap', cost_usd: 0.001 }),
        agent({ name: 'dear', cost_usd: 7.259 }),
        agent({ name: 'mid', cost_usd: 1.5 }),
      ]),
    )
    await mount()

    expect(
      Array.from(container.querySelectorAll('tbody tr')).map((r) => r.getAttribute('data-testid')),
    ).toEqual(['agent-row-dear', 'agent-row-mid', 'agent-row-cheap'])
  })

  it('CRITICAL: an unattributed role renders em-dashes, never a fabricated $0.000 or 0%', async () => {
    fetchAgents.mockResolvedValue(
      response([agent({ name: 'quiet', attributed: false, cost_usd: 0, success_rate: null, run_count: 0 })]),
    )
    await mount()

    const row = container.querySelector('[data-testid="agent-row-quiet"]')
    expect(row?.textContent).toContain('—')
    expect(row?.textContent).not.toContain('$0.000')
    expect(row?.textContent).not.toContain('0%')
    expect(container.querySelector('[data-testid="agent-no-activity-quiet"]')).not.toBeNull()
    // Quiet is not a fault: no severity stripe.
    expect(row?.className).not.toMatch(/border-l-\[var\(--color-(crit|warn)\)\]/)
  })

  it('CRITICAL: flags a role with a recent non-ACCEPT verdict, and never one with only accepts', async () => {
    fetchAgents.mockResolvedValue(
      response([
        agent({ name: 'rejecter', display_name: 'rejecter' }),
        agent({ name: 'accepter', display_name: 'accepter' }),
      ]),
    )
    fetchVerdicts.mockResolvedValue({
      verdicts: [],
      by_role: {
        rejecter: { decision_counts: { REJECT: 1 } },
        accepter: { decision_counts: { ACCEPT: 3 } },
      },
    } as unknown as VerdictsResponse)
    await mount()

    const band = container.querySelector('[data-testid="agents-attention-band"]')
    expect(band?.textContent).toContain('rejecter')
    expect(band?.textContent).not.toContain('accepter')
  })

  it('states an explicit all-clear rather than silently omitting the band', async () => {
    fetchAgents.mockResolvedValue(response([agent({ name: 'a' })]))
    await mount()

    expect(container.querySelector('[data-testid="agents-all-clear"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="agents-attention-band"]')).toBeNull()
  })

  it('renders the roster as a comparable table inside its own scroll container', async () => {
    fetchAgents.mockResolvedValue(response([agent({ name: 'a' }), agent({ name: 'b' })]))
    await mount()

    expect(container.querySelector('table')).not.toBeNull()
    expect(container.querySelector('[data-slot="table-container"]')?.className).toMatch(/overflow-x-auto/)
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2)
  })

  it('CRITICAL: activating a row opens that role detail drawer', async () => {
    fetchAgents.mockResolvedValue(response([agent({ name: 'developer' })]))
    await mount()

    await act(async () => {
      ;(container.querySelector('[data-testid="agent-row-developer"]') as HTMLElement).click()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
    expect(fetchLessons).toHaveBeenCalledWith('developer')
    expect(fetchVerdicts).toHaveBeenCalledWith('developer')
  })

  it('the attention band entry opens the same drawer', async () => {
    fetchAgents.mockResolvedValue(response([agent({ name: 'weak', success_rate: 0.1 })]))
    await mount()

    await act(async () => {
      ;(container.querySelector('[data-testid="agents-attention-weak"]') as HTMLElement).click()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
  })

  it('hides the unattributed bucket entirely when there is nothing unattributed', async () => {
    fetchAgents.mockResolvedValue(response([agent({})]))
    await mount()
    expect(container.querySelector('[data-testid="agents-unknown-bucket"]')).toBeNull()
  })

  it('shows the unattributed bucket, without the backend note, when it holds real activity', async () => {
    const data = response([agent({})])
    data.unknown.step_count = 12
    data.unknown.cost_usd = 0.5
    fetchAgents.mockResolvedValue(data)
    await mount()

    const bucket = container.querySelector('[data-testid="agents-unknown-bucket"]')
    expect(bucket).not.toBeNull()
    expect(bucket?.textContent).toContain('$0.500')
    expect(bucket?.textContent).not.toContain('steps.role')
  })

  it('an empty roster explains how one gets populated', async () => {
    fetchAgents.mockResolvedValue(response([]))
    await mount()

    const empty = container.querySelector('[data-testid="agents-empty"]')
    expect(empty).not.toBeNull()
    expect(empty?.textContent).toMatch(/roles\.yaml/)
  })

  it('a 403 on /v1/agents shows a graceful message, not a crash', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchAgents.mockRejectedValue(new ApiForbiddenError())
    await mount()

    expect(container.querySelector('[data-testid="agents-forbidden"]')).not.toBeNull()
  })

  it('a 403 on /v1/verdicts degrades the severity signal without hiding the roster', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchAgents.mockResolvedValue(response([agent({ name: 'a' })]))
    fetchVerdicts.mockRejectedValue(new ApiForbiddenError())
    await mount()

    expect(container.querySelector('[data-testid="agent-row-a"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="agents-verdicts-forbidden"]')).not.toBeNull()
  })

  it('CRITICAL: never uses dangerouslySetInnerHTML (role names, lessons and verdicts are untrusted text)', () => {
    expect(agentsViewSource).not.toContain('dangerouslySetInnerHTML')
  })
})
