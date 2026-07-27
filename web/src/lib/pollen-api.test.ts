import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, apiFetch: apiFetchMock }
})

import {
  fetchAgents,
  fetchAnalyticsCost,
  fetchAnalyticsDurations,
  fetchAnalyticsProviders,
  fetchAnalyticsSummary,
  fetchAnalyticsTrends,
  fetchApprovalLatency,
  fetchApprovals,
  fetchAutopilot,
  fetchEfficiency,
  fetchLessons,
  fetchMemories,
  fetchMemoryEvaluations,
  fetchMemoryGaps,
  fetchMemoryGrowth,
  fetchMemoryJournal,
  fetchMemoryReality,
  fetchModels,
  fetchRun,
  fetchVerdicts,
  parseGraphRunSelector,
  pauseAutopilot,
  postApproval,
  fetchPanel,
  fetchPanels,
  fetchPluginsHealth,
  fetchStepFailures,
  postJson,
  resumeAutopilot,
  whoami,
} from './pollen-api'

beforeEach(() => {
  apiFetchMock.mockReset()
  apiFetchMock.mockResolvedValue({})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('pollen-api fetch wrappers', () => {
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

  it('whoami calls GET /v1/whoami', async () => {
    await whoami()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/whoami')
  })

  it('postJson POSTs to path with JSON content-type, a stringified body, and on403: "forbidden"', async () => {
    // postJson delegates to apiFetch, which is what actually surfaces a 403
    // as ApiForbiddenError (see api.test.ts's "with on403: 'forbidden'..."
    // coverage) — this asserts postJson passes through the exact
    // method/headers/body/on403 shape that makes that guarantee hold.
    await postJson('/v1/approvals/42', { approve: true, reason: 'looks good' })
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/approvals/42', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approve: true, reason: 'looks good' }),
      on403: 'forbidden',
    })
  })

  it('fetchApprovals calls GET /v1/approvals and opts into on403: "forbidden"', async () => {
    // GET /v1/approvals requires `run` server-side, so a lower-rank token 403s
    // listing — the wrapper opts into on403:'forbidden' so ApprovalsView can
    // show a graceful message instead of clearing the token.
    await fetchApprovals()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/approvals', { on403: 'forbidden' })
  })

  it('postApproval POSTs {approver:"web", approve, reason} to /v1/approvals/{run_id}', async () => {
    // Asserts the exact ApprovalAction body contract (approver injected as
    // "web") + path + on403 — the raw request shape the view-level mock cannot
    // verify. postJson is real here; only apiFetch is mocked.
    await postApproval(42, { approve: false, reason: 'not this time' })
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/approvals/42', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approver: 'web', approve: false, reason: 'not this time' }),
      on403: 'forbidden',
    })
  })

  it('fetchMemoryReality calls GET /v1/memory/reality with a days window and opts into on403: "forbidden"', async () => {
    await fetchMemoryReality(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/memory/reality?days=30', { on403: 'forbidden' })
  })

  it('fetchMemoryGaps calls GET /v1/memory/gaps with a days window and opts into on403: "forbidden"', async () => {
    await fetchMemoryGaps(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/memory/gaps?days=30', { on403: 'forbidden' })
  })

  it('fetchMemoryEvaluations calls GET /v1/memory/evaluations with a limit and opts into on403: "forbidden"', async () => {
    await fetchMemoryEvaluations(50)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/memory/evaluations?limit=50', { on403: 'forbidden' })
  })

  it('fetchMemoryJournal calls GET /v1/memory/journal with a limit and opts into on403: "forbidden"', async () => {
    await fetchMemoryJournal(50)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/memory/journal?limit=50', { on403: 'forbidden' })
  })

  // ---- Mirador Home command-center sprint: /v1/models, /v1/efficiency,
  // /v1/memory/growth --------------------------------------------------

  it('fetchModels calls GET /v1/models with a days window', async () => {
    await fetchModels(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/models?days=30')
  })

  it('fetchModels adds project/task query params when given', async () => {
    await fetchModels(7, 'acme-web', 'deploy')
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/models?days=7&project=acme-web&task=deploy')
  })

  it('fetchEfficiency calls GET /v1/efficiency with a days window', async () => {
    await fetchEfficiency(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/efficiency?days=30')
  })

  it('fetchMemoryGrowth calls GET /v1/memory/growth with a days window and opts into on403: "forbidden"', async () => {
    await fetchMemoryGrowth(30)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/memory/growth?days=30', { on403: 'forbidden' })
  })

  // ---- Mirador Operate section: run detail drill-down (GET /v1/runs/{id}) ----

  it('fetchRun calls GET /v1/runs/{run_id} and opts into on403: "forbidden"', async () => {
    await fetchRun(42)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/runs/42', { on403: 'forbidden' })
  })

  // ---- Mirador Autopilot view (GET/POST /v1/autopilot) ----

  it('fetchAutopilot calls GET /v1/autopilot and opts into on403: "forbidden"', async () => {
    await fetchAutopilot()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/autopilot', { on403: 'forbidden' })
  })

  it('pauseAutopilot POSTs an empty body to /v1/autopilot/pause with on403: "forbidden"', async () => {
    await pauseAutopilot()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/autopilot/pause', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
      on403: 'forbidden',
    })
  })

  it('resumeAutopilot POSTs an empty body to /v1/autopilot/resume with on403: "forbidden"', async () => {
    await resumeAutopilot()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/autopilot/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
      on403: 'forbidden',
    })
  })

  // ---- Pollen Agents view (GET /v1/agents, /v1/lessons, /v1/verdicts) ----

  it('fetchAgents calls GET /v1/agents with no query string when no args given, opts into on403: "forbidden"', async () => {
    await fetchAgents()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/agents', { on403: 'forbidden' })
  })

  it('fetchAgents adds days/project/task query params when given', async () => {
    await fetchAgents(30, 'acme-web', 'deploy')
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/agents?days=30&project=acme-web&task=deploy', {
      on403: 'forbidden',
    })
  })

  it('fetchLessons calls GET /v1/lessons with a default limit and opts into on403: "forbidden"', async () => {
    await fetchLessons()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/lessons?limit=50', { on403: 'forbidden' })
  })

  it('fetchLessons adds a role filter when given', async () => {
    await fetchLessons('developer', 20)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/lessons?limit=20&role=developer', { on403: 'forbidden' })
  })

  it('fetchVerdicts calls GET /v1/verdicts with a default limit and opts into on403: "forbidden"', async () => {
    await fetchVerdicts()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/verdicts?limit=50', { on403: 'forbidden' })
  })

  it('fetchVerdicts adds a role filter and a custom limit when given', async () => {
    await fetchVerdicts('reviewer', 200)
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/verdicts?limit=200&role=reviewer', { on403: 'forbidden' })
  })

  describe('parseGraphRunSelector', () => {
    it('returns null when meta has no runs array at all (a different source, or an old response)', () => {
      expect(parseGraphRunSelector({})).toBeNull()
      expect(parseGraphRunSelector({ runs: 'not-an-array' })).toBeNull()
    })

    it('parses a well-formed run selector', () => {
      const result = parseGraphRunSelector({
        runs: [
          { id: 1, started_at: '2026-01-01 00:00:00', status: 'complete' },
          { id: 2, started_at: null, status: 'running' },
        ],
        selected_run_id: 2,
        live: true,
      })
      expect(result).toEqual({
        runs: [
          { id: 1, started_at: '2026-01-01 00:00:00', status: 'complete' },
          { id: 2, started_at: null, status: 'running' },
        ],
        selectedRunId: 2,
        live: true,
      })
    })

    it('drops malformed run entries (missing numeric id) instead of crashing', () => {
      const result = parseGraphRunSelector({ runs: [{ id: 'not-a-number' }, { id: 3 }, 'garbage', null] })
      expect(result?.runs).toEqual([{ id: 3, started_at: null, status: null }])
    })

    it('defaults selectedRunId to null and live to false when absent', () => {
      const result = parseGraphRunSelector({ runs: [] })
      expect(result).toEqual({ runs: [], selectedRunId: null, live: false })
    })
  })
})
