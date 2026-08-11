import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'

// Pollen wires seven real data views (Analytics/Cost/Health/Mem0/Approvals/
// Runs/Graph) — mock every endpoint they call so this test exercises the
// shell (sidebar nav, header, default view, switching) without depending on
// network behavior. Each view's own loading/error/empty/data states are
// covered by its dedicated test file. `fetchPluginsHealth` also backs the
// header's `StatusPills` (P0b) in addition to `HealthView` — one mock, both
// consumers.
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
  fetchAnalyticsProviders: vi.fn().mockResolvedValue({ by_provider: [], by_model: [] }),
  // Mirador Spend section sprint: ModelsView fetches its own /v1/models —
  // mocked genuinely-empty so this shell test exercises tab switching only,
  // not ModelsView's own data/empty/error states (covered by
  // ModelsView.test.tsx).
  fetchModels: vi.fn().mockResolvedValue({
    models: [],
    overall: { total_steps: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, unpriced_steps: 0, succeeded_runs: 0, cost_per_successful_run: null },
    latency_available: false,
    latency_note: 'p50/p95 latency is not computable from current data.',
  }),
  fetchPluginsHealth: vi.fn().mockResolvedValue({ plugins: [], disabled: [] }),
  fetchMemories: vi.fn().mockResolvedValue({ configured: true, memories: [] }),
  fetchPanels: vi.fn().mockResolvedValue({ panels: [] }),
  fetchPanel: vi.fn().mockResolvedValue({ sections: [] }),
  // Pollen now wraps its tree in RoleProvider (Sprint 1), which fetches
  // whoami() once on mount — mock it out like every other data source above
  // so this test exercises the shell only, not a real network call.
  whoami: vi.fn().mockResolvedValue({ role: 'admin', tenant: 'default' }),
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
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, ...mocks }
})

import { LANG_STORAGE_KEY } from '@/lib/i18n'
import { Pollen } from './Pollen'

// The sidebar's grouped nav order (P0b, + Home command-center sprint, +
// Mirador Spend section sprint, + Mirador Operate section sprint, +
// Mirador Memory unification sprint) — see `./nav/nav-config.ts`'s
// `NAV_GROUP_ORDER`: At a glance (Home), Operate (Runs/Approvals —
// renamed from "Agents", moved right after Home so the Run Board is the
// primary "what's happening" destination), Spend (Cost/Models/Efficiency),
// Overview (Analytics), Memory (ONE unified tab — see below), System
// (Health/Graph — demoted to LAST: the node-graph is no longer a prominent
// top-level destination, still fully reachable). Every built-in tab is
// still reachable, just reordered by group instead of the old flat
// declaration order.
//
// Memory unification sprint: the formerly-separate "Mem0" and "Memory > Quality"
// top-level tabs merged into ONE "Memory" tab, which itself has internal
// Quality/Growth/Search tabs (see `MemoryView.test.tsx` for coverage of
// that inner tab switching) — this shell-level list only asserts the ONE
// outer "Memory" entry is reachable, same as every other built-in.
//
// Group/tab labels below are the ENGLISH default (P1a: FR/EN i18n — see the
// "language toggle" describe block for the French-language assertions of
// the same shell).
const GROUPED_TAB_ORDER = [
  'Home',
  'Runs',
  'Approvals',
  // Propose -> ratify -> dispatch PRD, Sprint 4: Partitions joins the Operate
  // group, between Approvals and Autopilot (see nav-config.ts).
  'Partitions',
  'Autopilot',
  'Cost',
  'Models',
  'Efficiency',
  'Analytics',
  'Memory',
  'Health',
  // One card per curated plugin (GET /v1/plugins/catalog) — grouped under
  // System beside Health, which is where plugin state already lived.
  'Plugins',
  // Prompt-cache economics, beside Plugins under System. Separate from
  // Analytics on purpose: those aggregate, and an aggregate is what hid
  // 1.7M tokens of unread cache creation behind an 85% hit rate.
  'Cache',
  'Agents',
  'Graph',
]

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  window.localStorage.clear()
  for (const mock of Object.values(mocks)) mock.mockClear()
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

describe('Pollen', () => {
  it('renders the Pollen title and subtitle, and every tab reachable via the sidebar', () => {
    expect(container.textContent).toContain('Pollen')
    expect(container.textContent).toContain('HivePilot dashboard')
    // visual identity: the brand mark next to the wordmark.
    expect(container.querySelector('[data-slot="brand-mark"]')).not.toBeNull()
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(GROUPED_TAB_ORDER)
  })

  it('groups the sidebar into labelled sections (English default)', () => {
    expect(container.textContent).toContain('At a glance')
    expect(container.textContent).toContain('Operate')
    expect(container.textContent).toContain('Spend')
    expect(container.textContent).toContain('Overview')
    expect(container.textContent).toContain('System')
    expect(container.textContent).toContain('Memory')
  })

  it('CRITICAL: demotes the node-graph — "System" (Graph) is the LAST sidebar group, "Operate" (Runs) is right after Home', () => {
    const groupLabels = Array.from(container.querySelectorAll('[data-slot="sidebar-nav"] span.uppercase')).map(
      (el) => el.textContent,
    )
    expect(groupLabels[0]).toBe('At a glance')
    expect(groupLabels[1]).toBe('Operate')
    expect(groupLabels[groupLabels.length - 1]).toBe('System')

    // Graph is still fully reachable — just not prominent.
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toContain('Graph')
  })

  it('shows the real Home view by default', async () => {
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.textContent).toContain('Your fleet at a glance')
    const homeTab = container.querySelector('[role="tab"]')
    expect(homeTab?.textContent).toBe('Home')
    expect(homeTab?.getAttribute('aria-selected')).toBe('true')
  })

  it('switches to the real Analytics view when the Analytics item is clicked', async () => {
    const analyticsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Analytics',
    ) as HTMLElement

    await act(async () => {
      click(analyticsTab)
      await Promise.resolve()
    })

    expect(analyticsTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Volume & outcomes')
  })

  it('switches to the real Autopilot view when the Autopilot item is clicked (reachable via the sidebar)', async () => {
    const autopilotTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Autopilot',
    ) as HTMLElement
    expect(autopilotTab).not.toBeUndefined()

    await act(async () => {
      click(autopilotTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(autopilotTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Active')
  })

  it('switches to the real Cost view when the Cost item is clicked', async () => {
    const costTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement

    await act(async () => {
      click(costTab)
      await Promise.resolve()
    })

    expect(costTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Cost & tokens')
  })

  it('switches to the real Models view when the Models item is clicked', async () => {
    const modelsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Models',
    ) as HTMLElement

    await act(async () => {
      click(modelsTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(modelsTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Models')
    expect(panel?.textContent).toMatch(/no model data yet/i)
  })

  it('switches to the real Efficiency view when the Efficiency item is clicked', async () => {
    const efficiencyTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Efficiency',
    ) as HTMLElement

    await act(async () => {
      click(efficiencyTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(efficiencyTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Headroom')
  })

  it('switches to the real Health view when the Health item is clicked', async () => {
    const healthTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Health',
    ) as HTMLElement

    await act(async () => {
      click(healthTab)
      await Promise.resolve()
    })

    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Plugin health')
  })

  it('switches to the real Agents view when the Agents item is clicked (reachable via the sidebar)', async () => {
    const agentsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Agents',
    ) as HTMLElement
    expect(agentsTab).not.toBeUndefined()

    await act(async () => {
      click(agentsTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(agentsTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Agents')
    expect(mocks.fetchAgents).toHaveBeenCalled()
  })

  it('switches to the real Memory view when the Memory item is clicked, defaulting to its Quality tab', async () => {
    const memoryTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Memory',
    ) as HTMLElement

    await act(async () => {
      click(memoryTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(memoryTab.getAttribute('aria-selected')).toBe('true')
    // Default inner tab is Quality (the moved-in MemoryQualityView content) — its
    // own Quality/Growth/Search switching behavior is unit-tested in
    // MemoryView.test.tsx, this only proves the shell wiring.
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toMatch(/no memory activity recorded yet/i)
  })

  it('BUG FIX: the header hamburger and the sidebar drawer share the SAME breakpoint (md, not lg) — otherwise a realistic desktop window between 768-1023px would show a hidden hamburger to open the drawer but the sidebar itself could never dock statically', () => {
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

    const runsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Runs',
    ) as HTMLElement
    await act(async () => {
      click(runsTab)
      await Promise.resolve()
    })
    expect(nav.getAttribute('data-mobile-open')).toBe('false')
  })

  it('renders header status pills once plugin health resolves', async () => {
    mocks.fetchPluginsHealth.mockResolvedValue({
      plugins: [{ name: 'store', status: 'ok', detail: '' }],
      disabled: [],
    })

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

    const pills = container.querySelector('[data-testid="status-pills"]')
    expect(pills).not.toBeNull()
    expect(pills?.textContent).toContain('store')
  })

  it('never crashes the header when plugin health fails to load', async () => {
    mocks.fetchPluginsHealth.mockRejectedValue(new Error('boom'))

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
  })

  it('renders a theme toggle in the header that flips the .dark class', async () => {
    // No persisted theme and no pre-existing `.dark` class at mount time
    // (this file's top-level `beforeEach` clears both) — `useTheme` starts
    // from 'light' in that case (see `use-theme.test.tsx`), so the first
    // click flips to dark.
    const toggle = container.querySelector('[aria-label*="theme"]') as HTMLElement
    expect(toggle).not.toBeNull()
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    await act(async () => {
      click(toggle)
      await Promise.resolve()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    await act(async () => {
      click(toggle)
      await Promise.resolve()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('renders a language toggle in the header that switches the shell to French live and persists it', async () => {
    const langToggle = container.querySelector('[aria-label*="French"]') as HTMLElement
    expect(langToggle).not.toBeNull()
    expect(container.textContent).toContain('Overview')
    expect(container.textContent).not.toContain("Vue d'ensemble")

    await act(async () => {
      click(langToggle)
      await Promise.resolve()
    })

    expect(container.textContent).toContain("Vue d'ensemble")
    expect(container.textContent).toContain('Système')
    expect(container.textContent).toContain('Mémoire')
    expect(container.textContent).toContain('tableau de bord HivePilot')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('fr'))
  })

  // Command palette (P1b): CommandPalette.test.tsx unit-tests the palette's
  // own filtering/keyboard/i18n/focus behavior in isolation — these two
  // tests only prove the SHELL wiring: the header affordance opens the real
  // palette, and a real nav command actually flips `Pollen`'s (now
  // controlled) `Tabs` state and renders the target view.
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
  })

  it('switches the active view when a nav command is run from the command palette', async () => {
    const searchButton = container.querySelector('[aria-label="Search"]') as HTMLElement
    await act(async () => {
      click(searchButton)
      await Promise.resolve()
    })

    const input = document.body.querySelector('input') as HTMLInputElement
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      nativeSetter?.call(input, 'Cost')
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await Promise.resolve()
    })

    const costOption = Array.from(document.body.querySelectorAll('[role="option"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement
    await act(async () => {
      click(costOption)
      await Promise.resolve()
    })

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    const costTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement
    expect(costTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toContain('Cost & tokens')
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
    expect(tabs).toEqual([...GROUPED_TAB_ORDER, 'RTK Status', 'Secure Panel'])
    expect(container.textContent).toContain('Panels')
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
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const panelTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'RTK Status',
    ) as HTMLElement

    await act(async () => {
      click(panelTab)
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
      root.render(<Pollen />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const panelTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Secure Panel',
    ) as HTMLElement

    await act(async () => {
      click(panelTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    const forbidden = container.querySelector('[data-testid="panel-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(forbidden?.textContent).toMatch(/admin/i)
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('renders no extra items when fetchPanels resolves with an empty list', async () => {
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
    expect(tabs).toEqual(GROUPED_TAB_ORDER)
  })
})
