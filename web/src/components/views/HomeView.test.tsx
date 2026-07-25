import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import type {
  AnalyticsSummary,
  AnalyticsCost,
  Approval,
  EfficiencySummary,
  MemoryReality,
  RunSummary,
} from '@/lib/mirador-api'
import type { Role } from '@/lib/role-context'

const {
  fetchAnalyticsCost,
  fetchAnalyticsSummary,
  fetchApprovals,
  fetchEfficiency,
  fetchMemoryReality,
  fetchRuns,
  postApproval,
  useRoleMock,
} = vi.hoisted(() => ({
  fetchAnalyticsCost: vi.fn(),
  fetchAnalyticsSummary: vi.fn(),
  fetchApprovals: vi.fn(),
  fetchEfficiency: vi.fn(),
  fetchMemoryReality: vi.fn(),
  fetchRuns: vi.fn(),
  postApproval: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return {
    ...actual,
    fetchAnalyticsCost,
    fetchAnalyticsSummary,
    fetchApprovals,
    fetchEfficiency,
    fetchMemoryReality,
    fetchRuns,
    postApproval,
  }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { ApiForbiddenError } from '@/lib/api'
import { HomeView } from './HomeView'

function mockRole(role: Role) {
  const order: Role[] = ['read', 'run', 'approve', 'admin']
  useRoleMock.mockReturnValue({
    role,
    rank: order.indexOf(role),
    can: (required: Role) => order.indexOf(role) >= order.indexOf(required),
  })
}

const ZERO_COST: AnalyticsCost = {
  overall: { total_steps: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, unpriced_steps: 0 },
  by_provider: [],
  by_model: [],
  by_project: [],
  by_role: null,
  by_role_note: 'unavailable',
  unpriced_models: [],
}

const ZERO_SUMMARY: AnalyticsSummary = {
  total: 0,
  outcomes: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
  outcome_rates: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
  success_rate: null,
  by_project: {},
  by_task: {},
  by_raw_status: {},
}

const ZERO_EFFICIENCY: EfficiencySummary = {
  headroom: { total_compressions: 0, chars_saved: 0, avg_ratio: 0, p95_ratio: 0, est_tokens_saved: 0 },
  rtk: null,
}

const ZERO_MEMORY: MemoryReality = {
  search_success_rate: 0,
  total_searches: 0,
  no_result_count: 0,
  avg_freshness_seconds: 0,
  declared_reliability: 0,
  total_evaluations: 0,
}

function mockAllZero() {
  fetchAnalyticsCost.mockResolvedValue(ZERO_COST)
  fetchAnalyticsSummary.mockResolvedValue(ZERO_SUMMARY)
  fetchApprovals.mockResolvedValue([])
  fetchEfficiency.mockResolvedValue(ZERO_EFFICIENCY)
  fetchMemoryReality.mockResolvedValue(ZERO_MEMORY)
  fetchRuns.mockResolvedValue([])
}

const SAMPLE_APPROVAL: Approval = {
  run_id: 42,
  project: 'acme-web',
  task: 'deploy',
  status: 'pending',
  requested_at: '2020-01-01T00:00:00Z',
}

const SAMPLE_RUN: RunSummary = {
  id: 7,
  project: 'acme-web',
  task: 'deploy',
  status: 'running',
  started_at: '2020-01-01T00:00:00Z',
  finished_at: null,
}

let container: HTMLDivElement
let root: Root
let onNavigate: Mock<(view: string) => void>

function mount() {
  act(() => {
    root.render(<HomeView onNavigate={onNavigate} />)
  })
}

beforeEach(() => {
  for (const mock of [
    fetchAnalyticsCost,
    fetchAnalyticsSummary,
    fetchApprovals,
    fetchEfficiency,
    fetchMemoryReality,
    fetchRuns,
    postApproval,
    useRoleMock,
  ]) {
    mock.mockReset()
  }
  onNavigate = vi.fn<(view: string) => void>()
  mockRole('approve')
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

describe('HomeView — hero KPIs', () => {
  it('renders all 5 KPI cards from resolved endpoint data', async () => {
    fetchAnalyticsCost.mockResolvedValue({
      ...ZERO_COST,
      overall: { total_steps: 3, input_tokens: 100, output_tokens: 50, cost_usd: 1.5, unpriced_steps: 0 },
    })
    fetchAnalyticsSummary.mockResolvedValue({
      ...ZERO_SUMMARY,
      total: 4,
      success_rate: 0.75,
      outcomes: { succeeded: 3, failed: 1, skipped: 0, other: 0 },
    })
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    fetchEfficiency.mockResolvedValue({
      headroom: { total_compressions: 5, chars_saved: 4000, avg_ratio: 0.5, p95_ratio: 0.6, est_tokens_saved: 1000 },
      rtk: { gain_pct: 10, tokens_saved: 500, total_commands: 20, saved_series: [], top_commands: null },
    })
    fetchMemoryReality.mockResolvedValue({ ...ZERO_MEMORY, total_searches: 10, search_success_rate: 0.9 })
    fetchRuns.mockResolvedValue([SAMPLE_RUN])

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-kpi-spend"]')?.textContent).toContain('$1.500')
    expect(container.querySelector('[data-testid="home-kpi-tokens"]')?.textContent).toContain('1,500')
    expect(container.querySelector('[data-testid="home-kpi-runs"]')?.textContent).toContain('4')
    expect(container.querySelector('[data-testid="home-kpi-memory"]')?.textContent).toContain('90%')
    expect(container.querySelector('[data-testid="home-kpi-approvals"]')?.textContent).toContain('1')
  })

  it('clicking a KPI card navigates to the view that explains it', async () => {
    mockAllZero()

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const clickAndCheck = (testId: string, expected: string) => {
      onNavigate.mockClear()
      const el = container.querySelector(`[data-testid="${testId}"]`) as HTMLElement
      act(() => {
        el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      })
      expect(onNavigate).toHaveBeenCalledWith(expected)
    }

    clickAndCheck('home-kpi-spend', 'cost')
    clickAndCheck('home-kpi-tokens', 'analytics')
    clickAndCheck('home-kpi-runs', 'runs')
    clickAndCheck('home-kpi-memory', 'reality')
    clickAndCheck('home-kpi-approvals', 'approvals')
  })
})

describe('HomeView — honest empty states', () => {
  it('shows real zeros, never fabricated numbers, on a fresh install', async () => {
    mockAllZero()

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-kpi-spend"]')?.textContent).toContain('$0.000')
    expect(container.querySelector('[data-testid="home-kpi-runs"]')?.textContent).toContain('0')
    expect(container.querySelector('[data-testid="home-kpi-approvals"]')?.textContent).toContain('0')
    expect(container.querySelector('[data-testid="home-kpi-memory-nodata"]')).not.toBeNull()
  })

  it('shows "not available" for tokens saved when both headroom and rtk are empty/null', async () => {
    mockAllZero()

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const tokensCard = container.querySelector('[data-testid="home-kpi-tokens"]')
    expect(tokensCard?.textContent).toMatch(/not available/i)
  })
})

describe('HomeView — Needs attention', () => {
  it('renders pending approvals oldest-first with an inline approve action', async () => {
    mockAllZero()
    const older: Approval = { ...SAMPLE_APPROVAL, run_id: 1, requested_at: '2020-01-01T00:00:00Z' }
    const newer: Approval = { ...SAMPLE_APPROVAL, run_id: 2, requested_at: '2020-06-01T00:00:00Z' }
    fetchApprovals.mockResolvedValue([newer, older])
    postApproval.mockResolvedValue({ result: { success: true } })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const rows = Array.from(container.querySelectorAll('[data-testid^="home-attention-approval-"]'))
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'home-attention-approval-1',
      'home-attention-approval-2',
    ])

    const approveButton = container.querySelector('button[aria-label="Approve run 1"]') as HTMLButtonElement
    await act(async () => {
      approveButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    expect(postApproval).toHaveBeenCalledWith(1, { approve: true, reason: undefined })
  })

  it('collapses to "all clear" when there are no pending approvals or failed runs', async () => {
    mockAllZero()

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-all-clear"]')).not.toBeNull()
  })

  it('never shows "all clear" when there IS a pending approval', async () => {
    mockAllZero()
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-all-clear"]')).toBeNull()
    expect(container.querySelector('[data-testid="home-attention-approval-42"]')).not.toBeNull()
  })

  it('lists recent failed runs alongside pending approvals', async () => {
    mockAllZero()
    fetchRuns.mockResolvedValue([{ ...SAMPLE_RUN, id: 99, status: 'failed' }])

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-attention-run-99"]')).not.toBeNull()
  })
})

describe('HomeView — The Sweep', () => {
  it('maps run status to radar dot status and renders a legend with counts', async () => {
    mockAllZero()
    fetchRuns.mockResolvedValue([
      { ...SAMPLE_RUN, id: 1, status: 'running' },
      { ...SAMPLE_RUN, id: 2, status: 'awaiting_approval' },
      { ...SAMPLE_RUN, id: 3, status: 'failed' },
      { ...SAMPLE_RUN, id: 4, status: 'success' },
    ])

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-sweep"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="home-sweep-legend-running"]')?.textContent).toContain('1')
    expect(container.querySelector('[data-testid="home-sweep-legend-waiting"]')?.textContent).toContain('1')
    expect(container.querySelector('[data-testid="home-sweep-legend-failed"]')?.textContent).toContain('1')
    expect(container.querySelector('[data-testid="home-sweep-legend-idle"]')?.textContent).toContain('1')
  })

  it('shows an honest empty state when there are no runs', async () => {
    mockAllZero()

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-sweep-empty"]')).not.toBeNull()
  })
})

describe('HomeView — Activity feed', () => {
  it('renders escaped untrusted project/task text as literal text, never markup', async () => {
    mockAllZero()
    fetchRuns.mockResolvedValue([
      { ...SAMPLE_RUN, id: 1, project: '<script>alert(1)</script>', task: 'deploy' },
    ])

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const feed = container.querySelector('[data-testid="home-activity-feed"]')
    expect(feed?.textContent).toContain('<script>alert(1)</script>')
    expect(feed?.querySelector('script')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
  })

  it('shows most-recent-first ordering across merged runs + approvals', async () => {
    mockAllZero()
    fetchRuns.mockResolvedValue([{ ...SAMPLE_RUN, id: 1, started_at: '2020-01-01T00:00:00Z' }])
    fetchApprovals.mockResolvedValue([{ ...SAMPLE_APPROVAL, run_id: 2, requested_at: '2020-06-01T00:00:00Z' }])

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const items = Array.from(container.querySelectorAll('li[data-testid^="home-activity-"]'))
    expect(items[0]?.getAttribute('data-testid')).toBe('home-activity-approval-2')
  })

  it('shows an honest empty state when there is no activity yet', async () => {
    mockAllZero()

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-activity-empty"]')).not.toBeNull()
  })
})

describe('HomeView — 403 handling', () => {
  it('a 403 on approvals never crashes the page — KPI and needs-attention degrade gracefully', async () => {
    mockAllZero()
    fetchApprovals.mockRejectedValue(new ApiForbiddenError())
    mockRole('read')

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Home')
    expect(container.querySelector('[data-testid="home-kpi-approvals"]')?.textContent).toContain('—')
  })

  it('a 403 on runs never crashes the page — Sweep degrades gracefully', async () => {
    mockAllZero()
    fetchRuns.mockRejectedValue(new ApiForbiddenError())
    mockRole('read')

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Home')
    expect(container.querySelector('[data-testid="home-sweep-forbidden"]')).not.toBeNull()
  })

  it('a 403 on both approvals and runs collapses needs-attention to one graceful message', async () => {
    mockAllZero()
    fetchApprovals.mockRejectedValue(new ApiForbiddenError())
    fetchRuns.mockRejectedValue(new ApiForbiddenError())
    mockRole('read')

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="home-attention-forbidden"]')).not.toBeNull()
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })
})
