import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'

const {
  askConcierge,
  fetchAnalyticsCost,
  fetchAnalyticsSummary,
  fetchApprovals,
  fetchPluginsHealth,
  fetchRuns,
  fetchHealthProbes,
} = vi.hoisted(() => ({
  askConcierge: vi.fn(),
  fetchAnalyticsCost: vi.fn(),
  fetchAnalyticsSummary: vi.fn(),
  fetchApprovals: vi.fn(),
  fetchPluginsHealth: vi.fn(),
  fetchRuns: vi.fn(),
  fetchHealthProbes: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return {
    ...actual,
    askConcierge,
    fetchAnalyticsCost,
    fetchAnalyticsSummary,
    fetchApprovals,
    fetchPluginsHealth,
    fetchRuns,
    fetchHealthProbes,
  }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return {
    ...actual,
    useRole: () => ({
      role: 'admin',
      can: (needed: string) => ['read', 'run', 'approve', 'admin'].includes(needed),
    }),
  }
})

import { InboxView } from './InboxView'

let container: HTMLDivElement
let root: Root
const onNavigate = vi.fn()

beforeEach(() => {
  askConcierge.mockReset()
  fetchAnalyticsCost.mockReset().mockResolvedValue({
    overall: { total_steps: 1, input_tokens: 0, output_tokens: 0, cost_usd: 0.002, unpriced_steps: 0 },
    by_provider: [],
    by_model: [],
    by_project: [],
    by_role: null,
    by_role_note: '',
    unpriced_models: [],
  })
  fetchAnalyticsSummary.mockReset().mockResolvedValue({
    total: 1,
    outcomes: { succeeded: 1, failed: 0, skipped: 0, other: 0 },
    outcome_rates: { succeeded: 1, failed: 0, skipped: 0, other: 0 },
    success_rate: 1,
    by_project: {},
    by_task: {},
    by_raw_status: {},
  })
  fetchApprovals.mockReset().mockResolvedValue([])
  fetchPluginsHealth.mockReset().mockResolvedValue({
    plugins: [{ name: 'headroom', status: 'degraded', detail: 'slow', activity: null }],
    disabled: [],
  })
  fetchRuns.mockReset().mockResolvedValue([
    { id: 1, project: 'p', task: 't', status: 'failed', started_at: '2026-09-01T00:00:00Z' },
    { id: 2, project: 'p', task: 't', status: 'failed', started_at: '2026-09-02T00:00:00Z' },
  ])
  fetchHealthProbes.mockReset().mockResolvedValue({
    agent_surface: { state: 'ok', backend: 'opencode' },
    otel: { state: 'never_arrived', rows: 0, age_hours: null },
  })
  onNavigate.mockReset()
  window.localStorage.removeItem('hivepilot.webui.inbox-snapshot-open')
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('InboxView', () => {
  it('renders Inbox chrome, the Snapshot bandeau, and the concierge chat', async () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <InboxView onNavigate={onNavigate} />
        </LanguageProvider>,
      )
    })
    await flush()
    const page = container.querySelector('[data-testid="inbox-page"]')
    expect(page).not.toBeNull()
    expect(page?.textContent).toContain('Inbox')
    expect(page?.textContent).toMatch(/⌘K/)
    expect(container.querySelector('[data-testid="chat-input"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="inbox-snapshot"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="inbox-kpi-spend"]')?.textContent).toContain('$0.002')
    expect(container.querySelector('[data-testid="inbox-kpi-success"]')?.textContent).toContain('100%')
    expect(container.querySelector('[data-testid="inbox-kpi-pending"]')?.textContent).toContain('0')
    expect(container.querySelector('[data-testid="inbox-alerts-strip"]')?.textContent).toMatch(/3 issues/)
    expect(container.textContent).not.toMatch(/need you/i)
    expect(container.textContent).not.toMatch(/What's new/i)
    expect(container.textContent).not.toMatch(/Your fleet at a glance/)
    expect(container.querySelector('[data-slot="sweep-radar"]')).toBeNull()
  })

  it('collapses the Snapshot KPIs and issues strip', async () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <InboxView onNavigate={onNavigate} />
        </LanguageProvider>,
      )
    })
    await flush()
    await act(async () => {
      ;(container.querySelector('[data-testid="inbox-snapshot-toggle"]') as HTMLButtonElement).click()
    })
    expect(container.querySelector('[data-testid="inbox-kpi-spend"]')).toBeNull()
    expect(container.querySelector('[data-testid="inbox-alerts-strip"]')).toBeNull()
    expect(container.querySelector('[data-testid="chat-input"]')).not.toBeNull()
  })

  it('opens the Alerts door from the issues strip', async () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <InboxView onNavigate={onNavigate} />
        </LanguageProvider>,
      )
    })
    await flush()
    await act(async () => {
      ;(container.querySelector('[data-testid="inbox-alerts-strip"]') as HTMLButtonElement).click()
    })
    expect(onNavigate).toHaveBeenCalledWith('alerts')
  })
})
