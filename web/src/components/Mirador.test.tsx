import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'

// Mirador wires six real data views (Analytics/Cost/Health/Mem0/Approvals/Runs) — mock
// every endpoint they call so this test exercises the shell (tabs, default
// view, switching) without depending on network behavior. Each view's own
// loading/error/empty/data states are covered by its dedicated test file.
const mocks = vi.hoisted(() => ({
  fetchAnalyticsSummary: vi.fn().mockResolvedValue({
    total: 0,
    outcomes: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
    outcome_rates: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
    by_project: {},
    by_task: {},
    by_raw_status: {},
  }),
  fetchAnalyticsTrends: vi.fn().mockResolvedValue({ bucket: 'day', series: [] }),
  fetchAnalyticsDurations: vi.fn().mockResolvedValue({
    overall: { count: 0, min: 0, max: 0, avg: 0, p50: 0, p95: 0, p99: 0 },
    by_project: {},
    by_task: {},
  }),
  fetchStepFailures: vi.fn().mockResolvedValue({ hotspots: [] }),
  fetchApprovalLatency: vi.fn().mockResolvedValue({ count: 0, min: 0, max: 0, avg: 0, p50: 0, p95: 0, p99: 0 }),
  fetchAnalyticsCost: vi.fn().mockResolvedValue({
    overall: { total_steps: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, unpriced_steps: 0 },
    by_provider: [],
    by_model: [],
  }),
  fetchAnalyticsProviders: vi.fn().mockResolvedValue({ by_provider: [], by_model: [] }),
  fetchPluginsHealth: vi.fn().mockResolvedValue({ plugins: [], disabled: [] }),
  fetchMemories: vi.fn().mockResolvedValue({ configured: true, memories: [] }),
  fetchPanels: vi.fn().mockResolvedValue({ panels: [] }),
  fetchPanel: vi.fn().mockResolvedValue({ sections: [] }),
  // Mirador now wraps its tree in RoleProvider (Sprint 1), which fetches
  // whoami() once on mount — mock it out like every other data source above
  // so this test exercises the shell only, not a real network call.
  whoami: vi.fn().mockResolvedValue({ role: 'admin', tenant: 'default' }),
  // Mirador Graph View PRD, Sprint 3: the Graph tab's GraphView fetches its
  // own source list on mount — mocked empty so this shell test never makes
  // a real network call, same as every other built-in tab above.
  fetchGraphSources: vi.fn().mockResolvedValue({ sources: [] }),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, ...mocks }
})

import { Mirador } from './Mirador'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockClear()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => {
    root.render(<Mirador />)
  })
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
})

describe('Mirador', () => {
  it('renders the Mirador title and all seven tabs', () => {
    expect(container.textContent).toContain('Mirador')
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(['Analytics', 'Cost', 'Health', 'Mem0', 'Approvals', 'Runs', 'Graph'])
  })

  it('shows the real Analytics view by default', async () => {
    await act(async () => {
      await Promise.resolve()
    })
    expect(container.textContent).toContain('Volume & outcomes')
    const analyticsTab = container.querySelector('[role="tab"]')
    expect(analyticsTab?.getAttribute('aria-selected')).toBe('true')
  })

  it('switches to the real Cost view when the Cost tab is clicked', async () => {
    const costTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement

    await act(async () => {
      costTab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      costTab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(costTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Cost & tokens')
  })

  it('switches to the real Health view when the Health tab is clicked', async () => {
    const healthTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Health',
    ) as HTMLElement

    await act(async () => {
      healthTab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      healthTab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Plugin health')
  })

  it('switches to the real Mem0 view when the Mem0 tab is clicked', async () => {
    const mem0Tab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Mem0',
    ) as HTMLElement

    await act(async () => {
      mem0Tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      mem0Tab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Mem0 memory search')
  })
})

describe('Mirador — dynamic plugin panel tabs', () => {
  // The file-level `beforeEach` above already mounted a default Mirador
  // (all `fetchPanels`/`fetchPanel` mocks resolved to empty) into
  // `container`/`root` before this block's own `beforeEach` runs. Unmount
  // that default instance first so each test below can set its own
  // `fetchPanels`/`fetchPanel` resolutions and mount a fresh instance
  // without leaking the discarded one.
  beforeEach(() => {
    act(() => {
      root.unmount()
    })
    container.remove()
  })

  it('adds one tab per panel returned by fetchPanels, after the built-in tabs', async () => {
    mocks.fetchPanels.mockResolvedValue({
      panels: [
        { name: 'rtk-status', title: 'RTK Status', min_role: 'read' },
        { name: 'secure-panel', title: 'Secure Panel', min_role: 'admin' },
      ],
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Mirador />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual([
      'Analytics',
      'Cost',
      'Health',
      'Mem0',
      'Approvals',
      'Runs',
      'Graph',
      'RTK Status',
      'Secure Panel',
    ])
  })

  it('switches to a dynamic panel tab and renders its data via PanelRenderer', async () => {
    for (const mock of Object.values(mocks)) mock.mockClear()
    mocks.fetchPanels.mockResolvedValue({
      panels: [{ name: 'rtk-status', title: 'RTK Status', min_role: 'read' }],
    })
    mocks.fetchPanel.mockResolvedValue({
      sections: [{ kind: 'stat', label: 'Queue depth', value: '4', status: 'ok' }],
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Mirador />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const panelTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'RTK Status',
    ) as HTMLElement

    await act(async () => {
      panelTab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      panelTab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(mocks.fetchPanel).toHaveBeenCalledWith('rtk-status')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Queue depth')
    expect(panel?.textContent).toContain('4')
  })

  it('shows a graceful requires-token message for a 403 on an under-role panel tab', async () => {
    for (const mock of Object.values(mocks)) mock.mockClear()
    mocks.fetchPanels.mockResolvedValue({
      panels: [{ name: 'secure-panel', title: 'Secure Panel', min_role: 'admin' }],
    })
    mocks.fetchPanel.mockRejectedValue(new ApiForbiddenError())

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Mirador />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const panelTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Secure Panel',
    ) as HTMLElement

    await act(async () => {
      panelTab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      panelTab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    const forbidden = container.querySelector('[data-testid="panel-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(forbidden?.textContent).toMatch(/admin/i)
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('renders no extra tabs when fetchPanels resolves with an empty list', async () => {
    for (const mock of Object.values(mocks)) mock.mockClear()
    mocks.fetchPanels.mockResolvedValue({ panels: [] })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Mirador />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(['Analytics', 'Cost', 'Health', 'Mem0', 'Approvals', 'Runs', 'Graph'])
  })
})
