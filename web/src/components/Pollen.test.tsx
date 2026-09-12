import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'

// Pollen wires seven real data views (Analytics/Cost/Health/Memory/Approvals/
// Runs/Graph) — mock every endpoint they call so this test exercises the
// shell (sidebar nav, header, default view, switching) without depending on
// network behavior. Each view's own loading/error/empty/data states are
// covered by its dedicated test file. `fetchPluginsHealth` + `fetchRuns` +
// `fetchHealthProbes` also back the header issues chip (the Alerts feed).
const mocks = vi.hoisted(() => ({
  fetchAnalyticsSummary: vi.fn().mockResolvedValue({
    total: 0,
    outcomes: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
    outcome_rates: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
    success_rate: null,
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
    by_project: [],
    by_role: null,
    by_role_note: 'by_role is unavailable',
    unpriced_models: [],
  }),
  fetchAnalyticsWhales: vi.fn().mockResolvedValue({ whales: [], limit: 20 }),
  fetchAnalyticsProviders: vi.fn().mockResolvedValue({ by_provider: [], by_model: [] }),
  // Mirador Spend section sprint: ModelsView fetches its own /v1/models —
  // mocked genuinely-empty so this shell test exercises tab switching only,
  // not ModelsView's own data/empty/error states (covered by
  // ModelsView.test.tsx).
  fetchHostResources: vi.fn().mockResolvedValue({
    available: false,
    source: null,
    ram: null,
    cpu: null,
    disk: null,
    note: 'unavailable',
  }),
  fetchHostProcesses: vi.fn().mockResolvedValue({
    host: 'testhost',
    processes: [],
    note: 'none',
  }),
  fetchHostBrowser: vi.fn().mockResolvedValue({
    attached: false,
    base_url: 'http://127.0.0.1:9222',
    tabs: [],
    note: 'empty',
    error: null,
  }),
  fetchSandboxProvider: vi.fn().mockResolvedValue({
    configured: false,
    provider: null,
    decision: 'no-go',
    evaluated: ['docker', 'e2b', 'daytona', 'box'],
    note: 'none',
  }),
  fetchComputerSession: vi.fn().mockResolvedValue({
    attached: false,
    can_takeover: false,
    controller: null,
    note: 'none',
  }),
  fetchModels: vi.fn().mockResolvedValue({
    models: [],
    overall: { total_steps: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, unpriced_steps: 0, succeeded_runs: 0, cost_per_successful_run: null },
    latency_available: false,
    latency_note: 'p50/p95 latency is not computable from current data.',
  }),
  fetchPluginsHealth: vi.fn().mockResolvedValue({ plugins: [], disabled: [] }),
  fetchHealthProbes: vi.fn().mockResolvedValue({
    agent_surface: { state: 'not_configured', backend: null },
    otel: { state: 'never_arrived', rows: 0, age_hours: null },
  }),
  fetchMcpServers: vi.fn().mockResolvedValue({ servers: [], cost_note: '' }),
  fetchMcpCatalog: vi.fn().mockResolvedValue({ catalog: [] }),
  fetchTypedTools: vi.fn().mockResolvedValue({ tools: [] }),
  fetchPluginPacks: vi.fn().mockResolvedValue({ packs: [] }),
  fetchManagedCatalogs: vi.fn().mockResolvedValue({
    composio: { configured: false },
    pipedream: { configured: false },
  }),
  fetchPanels: vi.fn().mockResolvedValue({ panels: [] }),
  fetchPanel: vi.fn().mockResolvedValue({ sections: [] }),
  // Pollen now wraps its tree in RoleProvider (Sprint 1), which fetches
  // whoami() once on mount — mock it out like every other data source above
  // so this test exercises the shell only, not a real network call.
  whoami: vi.fn().mockResolvedValue({ role: 'admin', tenant: 'default' }),
  fetchPushConfig: vi.fn().mockResolvedValue({ enabled: false, vapid_public_key: null }),
  // Mirador Graph View PRD, Sprint 3: the Graph tab's GraphView fetches its
  // own source list on mount — mocked empty so this shell test never makes
  // a real network call, same as every other built-in tab above.
  fetchGraphSources: vi.fn().mockResolvedValue({ sources: [] }),
  // Memory > Quality tab: MemoryQualityView fetches all four `/v1/memory/*` endpoints on
  // mount — mocked to a genuinely-empty (but successful) response so this
  // shell test exercises tab switching only, not MemoryQualityView's own
  // data/empty/error states (covered by MemoryQualityView.test.tsx).
  fetchMemoryReality: vi.fn().mockResolvedValue({
    search_success_rate: 0,
    total_searches: 0,
    no_result_count: 0,
    avg_freshness_seconds: 0,
    declared_reliability: 0,
    total_evaluations: 0,
  }),
  fetchMemoryGaps: vi.fn().mockResolvedValue({ gaps: [] }),
  fetchMemoryEvaluations: vi.fn().mockResolvedValue({ evaluations: [] }),
  fetchMemoryJournal: vi.fn().mockResolvedValue({ journal: [] }),
  // Memory unification sprint: the Memory tab's Growth sub-tab fetches
  // `/v1/memory/growth` (only once that inner tab is actually selected —
  // see `MemoryView`'s docstring) — mocked genuinely-empty like every
  // other `/v1/memory/*` endpoint above.
  fetchMemoryGrowth: vi.fn().mockResolvedValue({
    total: 0,
    memories_by_namespace: [],
    growth_series: [],
    authorship: null,
    by_actor: [],
    source: 'mem0',
  }),
  // Home tab (default landing view): fetches its own
  // approvals/runs/efficiency/today's-summary — mocked genuinely-empty so
  // this shell test exercises tab switching only, not HomeView's own
  // data/empty/error states (covered by HomeView.test.tsx).
  fetchApprovals: vi.fn().mockResolvedValue([]),
  fetchRuns: vi.fn().mockResolvedValue([]),
  // Mirador Operate section: the Run Board's run detail drill-down fetches
  // GET /v1/runs/{id} only once a card is clicked -- not on shell mount --
  // but mocked here defensively so any interaction test never makes a real
  // network call.
  fetchRun: vi.fn().mockResolvedValue({
    run_id: 0,
    project: '',
    task: '',
    status: 'running',
    steps: [],
  }),
  fetchEfficiency: vi.fn().mockResolvedValue({
    headroom: { total_compressions: 0, chars_saved: 0, avg_ratio: 0, p95_ratio: 0, est_tokens_saved: 0 },
    rtk: null,
  }),
  // Mirador Autopilot view sprint: the Autopilot tab's GET /v1/autopilot —
  // mocked genuinely-empty/real-shaped so this shell test exercises tab
  // switching only, not AutopilotView's own data/empty/error states
  // (covered by AutopilotView.test.tsx).
  fetchAutopilot: vi.fn().mockResolvedValue({
    tenant: 'default',
    paused: false,
    queue: [],
    queue_depth: 0,
    budget_daily_usd: null,
    budget_spent_today: null,
    budget_remaining: null,
    recent_dispatches: [],
    auto_dispatch_allowlist: [],
  }),
  fetchSchedules: vi.fn().mockResolvedValue({ schedules: [] }),
  fetchRoutines: vi.fn().mockResolvedValue({ routines: [] }),
  // Mirador "Agents" view: GET /v1/agents (roster) + /v1/verdicts (severity
  // signal, fetched unfiltered on mount) — mocked genuinely-empty so this
  // shell test exercises tab switching only, not AgentsView's own
  // data/empty/error/XSS/severity states (covered by AgentsView.test.tsx).
  // GET /v1/lessons only fires once a role card is drilled into (not on
  // mount) but is mocked defensively like `fetchRun` above.
  fetchAgents: vi.fn().mockResolvedValue({ agents: [], unknown: {
    run_count: 0,
    step_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0,
    unpriced_steps: 0,
    success_rate: null,
    last_active: null,
  }, note: 'Per-role attribution requires steps.role.' }),
  fetchLessons: vi.fn().mockResolvedValue({ lessons: [], by_role: {} }),
  fetchVerdicts: vi.fn().mockResolvedValue({ verdicts: [], by_role: {} }),
  fetchRoles: vi.fn().mockResolvedValue({ roles: [] }),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, ...mocks }
})

import { LANG_STORAGE_KEY } from '@/lib/i18n'
import { Pollen } from './Pollen'

/** Visible sidebar: four doors + Plus destinations (English default). */
const SIDEBAR_TAB_ORDER = [
  'Inbox',
  'Approvals',
  'Runs',
  'Alerts',
  'Rooms',
  'Orchestrator',
  'Spend',
  'Memory',
  'System',
]

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  window.localStorage.clear()
  for (const mock of Object.values(mocks)) mock.mockClear()
  mocks.fetchPluginsHealth.mockResolvedValue({ plugins: [], disabled: [] })
  mocks.fetchHealthProbes.mockResolvedValue({
    agent_surface: { state: 'not_configured', backend: null },
    otel: { state: 'never_arrived', rows: 0, age_hours: null },
  })
  mocks.fetchRuns.mockResolvedValue([])
  mocks.fetchPanels.mockResolvedValue({ panels: [] })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => {
    root.render(<Pollen />)
  })
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
})

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

function tab(label: string): HTMLElement {
  return Array.from(container.querySelectorAll('[role="tab"]')).find((el) => el.textContent === label) as HTMLElement
}

async function runPaletteCommand(label: string) {
  const searchButton = container.querySelector('[aria-label="Search"]') as HTMLElement
  await act(async () => {
    click(searchButton)
    await Promise.resolve()
  })
  const input = document.body.querySelector('input') as HTMLInputElement
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  await act(async () => {
    nativeSetter?.call(input, label)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await Promise.resolve()
  })
  const option = Array.from(document.body.querySelectorAll('[role="option"]')).find(
    (el) => el.textContent === label,
  ) as HTMLElement
  await act(async () => {
    click(option)
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('Pollen', () => {
  it('renders the four doors + Plus tray, not the old grouped nav', () => {
    expect(container.textContent).toContain('Pollen')
    expect(container.querySelector('[data-slot="brand-mark"]')).not.toBeNull()
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(SIDEBAR_TAB_ORDER)
    expect(container.querySelector('[data-testid="sidebar-plus"]')).not.toBeNull()
    expect(container.textContent).toContain('Rest via ⌘K')
    expect(container.textContent).not.toContain('At a glance')
    expect(container.textContent).not.toContain('HivePilot dashboard')
  })

  it('lands on Inbox, not the Home command-center', async () => {
    await flush()
    expect(container.querySelector('[data-testid="inbox-page"]')).not.toBeNull()
    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Talk to the agents')
    expect(container.querySelector('[role="tabpanel"]')?.textContent).not.toContain('Your fleet at a glance')
    expect(tab('Inbox').getAttribute('aria-selected')).toBe('true')
  })

  it('switches to Approvals, Runs, and Alerts from the sidebar', async () => {
    await act(async () => {
      click(tab('Approvals'))
      await Promise.resolve()
    })
    expect(tab('Approvals').getAttribute('aria-selected')).toBe('true')

    await act(async () => {
      click(tab('Runs'))
      await Promise.resolve()
    })
    expect(tab('Runs').getAttribute('aria-selected')).toBe('true')

    await act(async () => {
      click(tab('Alerts'))
      await Promise.resolve()
    })
    expect(container.querySelector('[data-testid="alerts-page"]')).not.toBeNull()
  })

  it('Plus Spend opens Cost; Plus System opens Health; Plus Memory opens Memory', async () => {
    await act(async () => {
      click(tab('Spend'))
      await Promise.resolve()
    })
    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Cost & tokens')

    await act(async () => {
      click(tab('System'))
      await Promise.resolve()
    })
    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('System probes')

    await act(async () => {
      click(tab('Memory'))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="tabpanel"]')?.textContent).toMatch(/no memory activity recorded yet/i)
  })

  it('keeps Home and other lab views reachable via ⌘K only', async () => {
    expect(tab('Home')).toBeUndefined()
    expect(tab('Analytics')).toBeUndefined()
    expect(tab('Graph')).toBeUndefined()
    await runPaletteCommand('Home')
    expect(container.textContent).toContain('Your fleet at a glance')
  })

  it('the header hamburger and the sidebar drawer share the md breakpoint', () => {
    const hamburger = container.querySelector('[data-testid="mobile-nav-trigger"]') as HTMLElement
    expect(hamburger.className).toContain('md:hidden')
    expect(hamburger.className).not.toMatch(/\blg:hidden\b/)
  })

  it('opens the mobile nav drawer from the header hamburger, and closes it on item click', async () => {
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.getAttribute('data-mobile-open')).toBe('false')

    const hamburger = container.querySelector('[data-testid="mobile-nav-trigger"]') as HTMLElement
    await act(async () => {
      click(hamburger)
      await Promise.resolve()
    })
    expect(nav.getAttribute('data-mobile-open')).toBe('true')
    expect(container.querySelector('[data-testid="sidebar-backdrop"]')).not.toBeNull()

    await act(async () => {
      click(tab('Runs'))
      await Promise.resolve()
    })
    expect(nav.getAttribute('data-mobile-open')).toBe('false')
  })

  it('replaces plugin pills with a single issues chip', async () => {
    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
    await flush()
    expect(container.querySelector('[data-testid="issues-chip"]')?.textContent).toBe('All clear')
  })

  it('counts failed runs + degraded plugins on the chip and Alerts badge', async () => {
    mocks.fetchPluginsHealth.mockResolvedValue({
      plugins: [
        { name: 'store', status: 'ok', detail: '', activity_available: false, activity: null },
        { name: 'headroom', status: 'degraded', detail: 'slow', activity_available: false, activity: null },
      ],
      disabled: [],
    })
    mocks.fetchRuns.mockResolvedValue([
      { id: 1, project: 'p', task: 't', status: 'failed', started_at: '2026-01-01T00:00:00Z' },
      { id: 2, project: 'p', task: 't', status: 'failed', started_at: '2026-01-01T00:00:00Z' },
      { id: 3, project: 'p', task: 't', status: 'succeeded', started_at: '2026-01-01T00:00:00Z' },
    ])

    act(() => {
      root.unmount()
    })
    container.remove()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
    expect(container.querySelector('[data-testid="issues-chip"]')?.textContent).toBe('3 issues')
    expect(container.querySelector('[data-testid="alerts-badge"]')?.textContent).toBe('3')
  })

  it('never crashes the header when plugin health fails to load', async () => {
    mocks.fetchPluginsHealth.mockRejectedValue(new Error('boom'))
    mocks.fetchRuns.mockResolvedValue([])

    act(() => {
      root.unmount()
    })
    container.remove()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Pollen')
    expect(container.querySelector('[data-testid="status-pills"]')).toBeNull()
    expect(container.querySelector('[data-testid="issues-chip"]')?.textContent).toBe('All clear')
  })

  it('theme and language live in the overflow menu', async () => {
    expect(container.querySelector('[aria-label*="theme"]')).toBeNull()
    await act(async () => {
      click(container.querySelector('[data-testid="header-overflow"]') as HTMLElement)
      await Promise.resolve()
    })
    const toggle = container.querySelector('[aria-label*="theme"]') as HTMLElement
    expect(toggle).not.toBeNull()
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    await act(async () => {
      click(toggle)
      await Promise.resolve()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    const langToggle = container.querySelector('[aria-label*="French"]') as HTMLElement
    await act(async () => {
      click(langToggle)
      await Promise.resolve()
    })
    expect(container.textContent).toContain('Alertes')
    expect(container.textContent).toContain('Salles')
    expect(container.textContent).toContain('Le lab est à portée')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('fr'))
  })

  it('opens the command palette from the header search button', async () => {
    expect(container.querySelector('[role="dialog"]')).toBeNull()
    const searchButton = container.querySelector('[aria-label="Search"]') as HTMLElement
    expect(searchButton).not.toBeNull()

    await act(async () => {
      click(searchButton)
      await Promise.resolve()
    })
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(document.body.textContent).toContain('Cost')
    expect(document.body.textContent).toContain('Home')
  })

  it('switches the active view when a nav command is run from the command palette', async () => {
    await runPaletteCommand('Cost')
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(tab('Spend').getAttribute('aria-selected')).toBe('true')
    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Cost & tokens')
  })
})

describe('Pollen — dynamic plugin panel tabs', () => {
  // The file-level `beforeEach` above already mounted a default Pollen
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

  it('adds one item per panel returned by fetchPanels, after the grouped built-in items', async () => {
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
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(SIDEBAR_TAB_ORDER)
    expect(tabs).not.toContain('RTK Status')

    const searchButton = container.querySelector('[aria-label="Search"]') as HTMLElement
    await act(async () => {
      click(searchButton)
      await Promise.resolve()
    })
    expect(document.body.textContent).toContain('RTK Status')
    expect(document.body.textContent).toContain('Secure Panel')
    expect(document.body.textContent).toContain('Panels')
  })

  it('opens a dynamic panel from the command palette and renders its data', async () => {
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
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    await runPaletteCommand('RTK Status')
    expect(mocks.fetchPanel).toHaveBeenCalledWith('rtk-status')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Queue depth')
    expect(panel?.textContent).toContain('4')
  })

  it('shows a graceful requires-token message for a 403 on an under-role panel', async () => {
    for (const mock of Object.values(mocks)) mock.mockClear()
    mocks.fetchPanels.mockResolvedValue({
      panels: [{ name: 'secure-panel', title: 'Secure Panel', min_role: 'admin' }],
    })
    mocks.fetchPanel.mockRejectedValue(new ApiForbiddenError())

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    await runPaletteCommand('Secure Panel')
    const forbidden = container.querySelector('[data-testid="panel-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(forbidden?.textContent).toMatch(/admin/i)
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('renders no extra sidebar items when fetchPanels resolves with an empty list', async () => {
    for (const mock of Object.values(mocks)) mock.mockClear()
    mocks.fetchPanels.mockResolvedValue({ panels: [] })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(SIDEBAR_TAB_ORDER)
  })
})
