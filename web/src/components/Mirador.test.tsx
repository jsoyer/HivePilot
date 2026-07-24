import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'

// Mirador wires seven real data views (Analytics/Cost/Health/Mem0/Approvals/
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
  // Réalité tab: RealityView fetches all four `/v1/memory/*` endpoints on
  // mount — mocked to a genuinely-empty (but successful) response so this
  // shell test exercises tab switching only, not RealityView's own
  // data/empty/error states (covered by RealityView.test.tsx).
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
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, ...mocks }
})

import { LANG_STORAGE_KEY } from '@/lib/i18n'
import { Mirador } from './Mirador'

// The sidebar's grouped nav order (P0b) — see `./nav/nav-config.ts`'s
// `NAV_GROUP_ORDER`: Overview (Analytics/Cost), Agents (Approvals/Runs),
// System (Health/Graph), Memory (Mem0/Réalité). Every built-in tab is still
// reachable, just reordered by group instead of the old flat declaration
// order. Group/tab labels below are the ENGLISH default (P1a: FR/EN i18n —
// see the "language toggle" describe block for the French-language
// assertions of the same shell). "Réalité" itself is a proper noun, kept
// untranslated in both dictionaries (see en.ts/fr.ts's `nav.reality`).
const GROUPED_TAB_ORDER = ['Analytics', 'Cost', 'Approvals', 'Runs', 'Health', 'Graph', 'Mem0', 'Réalité']

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  window.localStorage.clear()
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
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
})

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('Mirador', () => {
  it('renders the Mirador title and subtitle, and every tab reachable via the sidebar', () => {
    expect(container.textContent).toContain('Mirador')
    expect(container.textContent).toContain('HivePilot insight dashboard')
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(GROUPED_TAB_ORDER)
  })

  it('groups the sidebar into labelled sections (English default)', () => {
    expect(container.textContent).toContain('Overview')
    expect(container.textContent).toContain('Agents')
    expect(container.textContent).toContain('System')
    expect(container.textContent).toContain('Memory')
  })

  it('shows the real Analytics view by default', async () => {
    await act(async () => {
      await Promise.resolve()
    })
    expect(container.textContent).toContain('Volume & outcomes')
    const analyticsTab = container.querySelector('[role="tab"]')
    expect(analyticsTab?.getAttribute('aria-selected')).toBe('true')
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

  it('switches to the real Mem0 view when the Mem0 item is clicked', async () => {
    const mem0Tab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Mem0',
    ) as HTMLElement

    await act(async () => {
      click(mem0Tab)
      await Promise.resolve()
    })

    expect(container.querySelector('[role="tabpanel"]')?.textContent).toContain('Mem0 memory search')
  })

  it('switches to the real Réalité view when the Réalité item is clicked', async () => {
    const realityTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Réalité',
    ) as HTMLElement

    await act(async () => {
      click(realityTab)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(realityTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toMatch(/no memory activity recorded yet/i)
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
      root.render(<Mirador />)
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
      root.render(<Mirador />)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Mirador')
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
    expect(container.textContent).toContain('Tableau de bord HivePilot')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe(JSON.stringify('fr'))
  })

  // Command palette (P1b): CommandPalette.test.tsx unit-tests the palette's
  // own filtering/keyboard/i18n/focus behavior in isolation — these two
  // tests only prove the SHELL wiring: the header affordance opens the real
  // palette, and a real nav command actually flips `Mirador`'s (now
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
      root.render(<Mirador />)
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
      root.render(<Mirador />)
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
      root.render(<Mirador />)
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
      root.render(<Mirador />)
      await Promise.resolve()
      await Promise.resolve()
    })

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(GROUPED_TAB_ORDER)
  })
})
