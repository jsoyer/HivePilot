import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, apiFetch: apiFetchMock }
})

import {
  fetchAnalyticsCost,
  fetchAnalyticsDurations,
  fetchAnalyticsProviders,
  fetchAnalyticsSummary,
  fetchAnalyticsTrends,
  fetchApprovalLatency,
  fetchMemories,
  fetchPanel,
  fetchPanels,
  fetchPluginsHealth,
  fetchStepFailures,
} from './mirador-api'

beforeEach(() => {
  apiFetchMock.mockReset()
  apiFetchMock.mockResolvedValue({})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('mirador-api fetch wrappers', () => {
  it('fetchAnalyticsSummary calls GET /v1/analytics/summary with a days window', async () => {
    await fetchAnalyticsSummary(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/summary?days=30')
  })

  it('fetchAnalyticsTrends calls GET /v1/analytics/trends?bucket=day', async () => {
    await fetchAnalyticsTrends(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/trends?days=30&bucket=day')
  })

  it('fetchAnalyticsDurations calls GET /v1/analytics/durations', async () => {
    await fetchAnalyticsDurations(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/durations?days=30')
  })

  it('fetchStepFailures calls GET /v1/analytics/steps/failures with a hotspot limit', async () => {
    await fetchStepFailures(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/steps/failures?days=30&limit=20')
  })

  it('fetchApprovalLatency calls GET /v1/analytics/approvals/latency', async () => {
    await fetchApprovalLatency(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/approvals/latency?days=30')
  })

  it('fetchAnalyticsProviders calls GET /v1/analytics/providers', async () => {
    await fetchAnalyticsProviders(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/providers?days=30')
  })

  it('fetchAnalyticsCost calls GET /v1/analytics/cost', async () => {
    await fetchAnalyticsCost(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/analytics/cost?days=30')
  })

  it('fetchPluginsHealth calls GET /v1/plugins/health', async () => {
    await fetchPluginsHealth()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/plugins/health')
  })

  it('fetchMemories calls GET /v1/memories with query/limit and opts into on403: "forbidden"', async () => {
    await fetchMemories('deploy', 20)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/memories?query=deploy&limit=20', {
      on403: 'forbidden',
    })
  })

  it('fetchMemories URL-encodes the query text', async () => {
    await fetchMemories('rate limit / retry', 10)
    const [url] = apiFetchMock.mock.calls[0] as [string]
    expect(url).toContain('query=rate+limit+%2F+retry')
    expect(url).not.toContain(' ')
  })

  it('fetchPanels calls GET /v1/panels', async () => {
    await fetchPanels()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/panels')
  })

  it('fetchPanel calls GET /v1/panels/{name} and opts into on403: "forbidden"', async () => {
    await fetchPanel('rtk-status')
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/panels/rtk-status', { on403: 'forbidden' })
  })

  it('fetchPanel URL-encodes the panel name', async () => {
    await fetchPanel('weird name/slash')
    const [url] = apiFetchMock.mock.calls[0] as [string]
    expect(url).toBe('/v1/panels/weird%20name%2Fslash')
  })
})
