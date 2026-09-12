import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { HealthProbes, PluginsHealthResponse, RunSummary } from '@/lib/pollen-api'
import { AlertsView } from './AlertsView'

const { fetchPluginsHealth, fetchRuns, fetchHealthProbes } = vi.hoisted(() => ({
  fetchPluginsHealth: vi.fn(),
  fetchRuns: vi.fn(),
  fetchHealthProbes: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchPluginsHealth, fetchRuns, fetchHealthProbes }
})

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchPluginsHealth.mockReset()
  fetchRuns.mockReset()
  fetchHealthProbes.mockReset()
  fetchPluginsHealth.mockResolvedValue({ plugins: [], disabled: [] } satisfies PluginsHealthResponse)
  fetchRuns.mockResolvedValue([] satisfies RunSummary[])
  fetchHealthProbes.mockResolvedValue({
    agent_surface: { state: 'not_configured', backend: null },
    otel: { state: 'never_arrived', rows: 0, age_hours: null },
  } satisfies HealthProbes)
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

async function renderAlerts() {
  await act(async () => {
    root.render(
      <LanguageProvider>
        <AlertsView />
      </LanguageProvider>,
    )
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('AlertsView', () => {
  it('renders a sober empty state when the feed is clear', async () => {
    await renderAlerts()
    expect(container.querySelector('[data-testid="alerts-page"]')).not.toBeNull()
    expect(container.textContent).toContain('Alerts')
    expect(container.textContent).toContain('Worst first · same feed as Telegram')
    expect(container.querySelector('[data-testid="alerts-empty"]')?.textContent).toBe(
      'All clear. Nothing in the feed.',
    )
    expect(container.querySelector('[data-testid="alerts-list"]')).toBeNull()
    expect(container.textContent).not.toMatch(/follow-up/i)
  })

  it('lists Failed, Degraded, then Classifier — no Health dump', async () => {
    fetchPluginsHealth.mockResolvedValue({
      plugins: [
        {
          name: 'headroom',
          status: 'degraded',
          detail: 'installed but disabled (headroom_enabled=False)',
          activity_available: false,
          activity: null,
        },
        {
          name: 'hugo',
          status: 'degraded',
          detail: 'not usable: hugo not on PATH — install Hugo to use this runner',
          activity_available: false,
          activity: null,
        },
        { name: 'store', status: 'ok', detail: 'ok', activity_available: false, activity: null },
      ],
      disabled: [],
    } satisfies PluginsHealthResponse)
    fetchRuns.mockResolvedValue([
      {
        id: 7690,
        project: 'noxxy',
        task: 'groomer-scan',
        status: 'failed',
        started_at: '2026-09-03T00:00:00Z',
        detail: 'operation expired — must not render',
      },
      {
        id: 7610,
        project: 'noxxy',
        task: 'groomer-scan',
        status: 'failed',
        started_at: '2026-08-26T00:00:00Z',
      },
    ] satisfies RunSummary[])
    fetchHealthProbes.mockResolvedValue({
      agent_surface: { state: 'ok', backend: 'opencode' },
      otel: { state: 'ok', rows: 12, age_hours: 0.1 },
    } satisfies HealthProbes)

    await renderAlerts()

    const rows = Array.from(container.querySelectorAll('[data-testid="alert-row"]'))
    expect(rows.map((row) => row.getAttribute('data-kind'))).toEqual([
      'failed',
      'failed',
      'degraded',
      'degraded',
      'classifier',
    ])
    expect(container.textContent).toContain('groomer-scan #7690')
    expect(container.textContent).toContain('groomer-scan #7610')
    expect(container.textContent).toContain('noxxy')
    expect(container.textContent).toContain('headroom')
    expect(container.textContent).toContain('installed but disabled')
    expect(container.textContent).toContain('hugo')
    expect(container.textContent).toMatch(/not on PATH/)
    expect(container.textContent).toContain('Concierge healthy')
    expect(container.textContent).toContain('opencode')
    expect(container.textContent).not.toMatch(/operation expired/)
    expect(container.textContent).not.toMatch(/OTel|database|never run|verdict/i)
  })

  it('uses soft kind colors — no glow, no decorative grid', async () => {
    fetchPluginsHealth.mockResolvedValue({
      plugins: [{ name: 'hugo', status: 'degraded', detail: 'not on PATH', activity_available: false, activity: null }],
      disabled: [],
    } satisfies PluginsHealthResponse)
    fetchRuns.mockResolvedValue([
      { id: 1, project: 'p', task: 't', status: 'failed', started_at: '2026-09-01T00:00:00Z' },
    ] satisfies RunSummary[])
    fetchHealthProbes.mockResolvedValue({
      agent_surface: { state: 'ok', backend: 'orca' },
      otel: { state: 'never_arrived', rows: 0, age_hours: null },
    } satisfies HealthProbes)

    await renderAlerts()

    const failed = container.querySelector('[data-kind="failed"]')
    const degraded = container.querySelector('[data-kind="degraded"]')
    const classifier = container.querySelector('[data-kind="classifier"]')
    expect(failed?.className).not.toMatch(/shadow|glow|bg-grid/)
    expect(degraded?.className).not.toMatch(/shadow|glow|bg-grid/)
    expect(classifier?.className).not.toMatch(/shadow|glow|bg-grid/)
    expect(failed?.textContent).toMatch(/Failed/)
    expect(degraded?.textContent).toMatch(/Degraded/)
    expect(classifier?.textContent).toMatch(/Classifier/)
    const kindClass = (row: Element | null) => row?.querySelector('span')?.className ?? ''
    expect(kindClass(failed)).toContain('--color-crit')
    expect(kindClass(degraded)).toContain('--color-warn')
    expect(kindClass(classifier)).toContain('text-primary')
  })
})
