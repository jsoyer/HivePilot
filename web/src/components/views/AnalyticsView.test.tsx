import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type {
  AnalyticsDurations,
  AnalyticsSummary,
  AnalyticsTrends,
  ApprovalLatency,
  StepFailuresResponse,
} from '@/lib/mirador-api'

const mocks = vi.hoisted(() => ({
  fetchAnalyticsSummary: vi.fn(),
  fetchAnalyticsTrends: vi.fn(),
  fetchAnalyticsDurations: vi.fn(),
  fetchStepFailures: vi.fn(),
  fetchApprovalLatency: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, ...mocks }
})

import { AnalyticsView } from './AnalyticsView'

let container: HTMLDivElement
let root: Root

const summary: AnalyticsSummary = {
  total: 12,
  outcomes: { succeeded: 9, failed: 2, skipped: 0, other: 1 },
  outcome_rates: { succeeded: 0.75, failed: 0.1667, skipped: 0, other: 0.0833 },
  by_project: {},
  by_task: {},
  by_raw_status: {},
}

const trends: AnalyticsTrends = {
  bucket: 'day',
  series: [
    { bucket: '2026-07-14', total: 4, outcomes: { succeeded: 3, failed: 1, skipped: 0, other: 0 } },
    { bucket: '2026-07-15', total: 8, outcomes: { succeeded: 6, failed: 1, skipped: 0, other: 1 } },
  ],
}

const durations: AnalyticsDurations = {
  overall: { count: 12, min: 1, max: 30, avg: 8, p50: 6, p95: 25, p99: 29 },
  by_project: {},
  by_task: {},
}

const hotspots: StepFailuresResponse = {
  hotspots: [{ step: 'deploy', status: 'failed', count: 3 }],
}

const approvalLatency: ApprovalLatency = { count: 5, min: 10, max: 120, avg: 45, p50: 40, p95: 100, p99: 118 }

function mount() {
  act(() => {
    root.render(<AnalyticsView />)
  })
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset()
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

describe('AnalyticsView', () => {
  it('shows loading indicators before any endpoint resolves', () => {
    for (const mock of Object.values(mocks)) mock.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelectorAll('[role="status"]').length).toBeGreaterThan(0)
  })

  it('renders volume, outcome rates, trend, duration percentiles, hotspots, and approval latency once loaded', async () => {
    mocks.fetchAnalyticsSummary.mockResolvedValue(summary)
    mocks.fetchAnalyticsTrends.mockResolvedValue(trends)
    mocks.fetchAnalyticsDurations.mockResolvedValue(durations)
    mocks.fetchStepFailures.mockResolvedValue(hotspots)
    mocks.fetchApprovalLatency.mockResolvedValue(approvalLatency)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('12')
    expect(container.textContent).toContain('75%')
    expect(container.querySelectorAll('svg rect').length).toBe(2)
    expect(container.textContent).toContain('deploy')
    expect(container.textContent).toContain('failed')
    expect(container.textContent).toMatch(/40\.00s/)
  })

  it('shows an empty state for the volume section when total is 0', async () => {
    mocks.fetchAnalyticsSummary.mockResolvedValue({
      total: 0,
      outcomes: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
      outcome_rates: { succeeded: 0, failed: 0, skipped: 0, other: 0 },
      by_project: {},
      by_task: {},
      by_raw_status: {},
    } satisfies AnalyticsSummary)
    mocks.fetchAnalyticsTrends.mockResolvedValue({ bucket: 'day', series: [] } satisfies AnalyticsTrends)
    mocks.fetchAnalyticsDurations.mockResolvedValue({
      overall: { count: 0, min: 0, max: 0, avg: 0, p50: 0, p95: 0, p99: 0 },
      by_project: {},
      by_task: {},
    } satisfies AnalyticsDurations)
    mocks.fetchStepFailures.mockResolvedValue({ hotspots: [] } satisfies StepFailuresResponse)
    mocks.fetchApprovalLatency.mockResolvedValue({
      count: 0,
      min: 0,
      max: 0,
      avg: 0,
      p50: 0,
      p95: 0,
      p99: 0,
    } satisfies ApprovalLatency)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no runs recorded/i)
  })

  it('shows an error card for a section whose endpoint rejects, without breaking the others', async () => {
    mocks.fetchAnalyticsSummary.mockResolvedValue(summary)
    mocks.fetchAnalyticsTrends.mockRejectedValue(new Error('trends unavailable'))
    mocks.fetchAnalyticsDurations.mockResolvedValue(durations)
    mocks.fetchStepFailures.mockResolvedValue(hotspots)
    mocks.fetchApprovalLatency.mockResolvedValue(approvalLatency)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toContain('trends unavailable')
    // The other sections still rendered their data — one failure doesn't
    // blank the whole panel.
    expect(container.textContent).toContain('deploy')
  })

  it('renders French card titles and StatCard labels when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mocks.fetchAnalyticsSummary.mockResolvedValue(summary)
    mocks.fetchAnalyticsTrends.mockResolvedValue(trends)
    mocks.fetchAnalyticsDurations.mockResolvedValue(durations)
    mocks.fetchStepFailures.mockResolvedValue(hotspots)
    mocks.fetchApprovalLatency.mockResolvedValue(approvalLatency)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <AnalyticsView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Volume et résultats')
    expect(container.textContent).toContain('Exécutions totales')
    expect(container.textContent).toContain('Réussies')
  })
})
