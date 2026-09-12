import { describe, expect, it } from 'vitest'
import { buildAlertFeed, countAlertIssues } from './alert-feed'

describe('buildAlertFeed', () => {
  it('orders Failed, then Degraded, then Classifier (worst first)', () => {
    const feed = buildAlertFeed({
      plugins: [
        {
          name: 'headroom',
          status: 'degraded',
          detail: 'installed but disabled (headroom_enabled=False)',
          activity: null,
        },
        { name: 'store', status: 'ok', detail: 'ok', activity: null },
      ],
      runs: [
        { id: 7610, project: 'noxxy', task: 'groomer-scan', status: 'failed', started_at: '2026-08-26T00:00:00Z' },
        { id: 7690, project: 'noxxy', task: 'groomer-scan', status: 'failed', started_at: '2026-09-03T00:00:00Z' },
        { id: 8000, project: 'noxxy', task: 'docs', status: 'success', started_at: '2026-09-10T00:00:00Z' },
      ],
      agentSurface: { state: 'ok', backend: 'opencode' },
    })
    expect(feed.map((item) => item.kind)).toEqual(['failed', 'failed', 'degraded', 'classifier'])
    expect(feed[0]?.title).toBe('groomer-scan #7690')
    expect(feed[1]?.title).toBe('groomer-scan #7610')
    expect(feed[2]?.title).toBe('headroom')
    expect(feed[2]?.meta).toContain('installed but disabled')
    expect(feed[3]?.classifierState).toBe('ok')
    expect(feed[3]?.countsAsIssue).toBe(false)
  })

  it('never copies untrusted run detail onto a row', () => {
    const feed = buildAlertFeed({
      runs: [
        {
          id: 1,
          project: 'noxxy',
          task: 'groomer-scan',
          status: 'failed',
          started_at: '2026-09-01T00:00:00Z',
          detail: 'operation expired — do not show this',
        },
      ],
    })
    expect(feed).toHaveLength(1)
    expect(feed[0]?.meta).toBe('noxxy')
    expect(JSON.stringify(feed)).not.toMatch(/operation expired/)
  })

  it('counts every FAILED_STATUSES member, not only literal failed', () => {
    const feed = buildAlertFeed({
      runs: [
        { id: 1, project: 'p', task: 't', status: 'denied', started_at: '2026-09-01T00:00:00Z' },
        { id: 2, project: 'p', task: 't', status: 'rate_limit', started_at: '2026-09-02T00:00:00Z' },
        { id: 3, project: 'p', task: 't', status: 'waiting_approval', started_at: '2026-09-03T00:00:00Z' },
      ],
    })
    expect(feed.map((item) => item.id)).toEqual(['failed-2', 'failed-1'])
  })

  it('surfaces PATH-gated plugin detail as Degraded meta', () => {
    const feed = buildAlertFeed({
      plugins: [
        {
          name: 'hugo',
          status: 'degraded',
          detail: 'not usable: hugo not on PATH — install Hugo to use this runner',
          activity: null,
        },
      ],
    })
    expect(feed[0]?.kind).toBe('degraded')
    expect(feed[0]?.title).toBe('hugo')
    expect(feed[0]?.meta).toMatch(/not on PATH/)
  })

  it('adds installed-but-disabled names that are not loaded and not merely uninstalled', () => {
    const feed = buildAlertFeed({
      plugins: [],
      disabled: ['obsidian', 'phantom'],
      notInstalled: ['phantom'],
    })
    expect(feed).toHaveLength(1)
    expect(feed[0]?.id).toBe('disabled-obsidian')
    expect(feed[0]?.metaKey).toBe('installedDisabled')
  })

  it('does not double-count a loaded plugin that is also pending-disable', () => {
    const feed = buildAlertFeed({
      plugins: [{ name: 'rtk', status: 'ok', detail: 'ok', activity: null }],
      disabled: ['rtk'],
    })
    expect(feed).toHaveLength(0)
  })

  it('includes denied plugins as Degraded', () => {
    const feed = buildAlertFeed({
      denied: [{ name: 'token_savior', error: 'capability denied' }],
    })
    expect(feed[0]?.kind).toBe('degraded')
    expect(feed[0]?.title).toBe('token_savior')
    expect(feed[0]?.meta).toBe('capability denied')
  })

  it('omits classifier when the surface is not configured or unknown', () => {
    expect(buildAlertFeed({ agentSurface: { state: 'not_configured', backend: null } })).toEqual([])
    expect(buildAlertFeed({ agentSurface: { state: 'unknown', backend: null } })).toEqual([])
    expect(buildAlertFeed({ agentSurface: { state: 'unknown_backend', backend: 'typo' } })).toEqual(
      [],
    )
    expect(buildAlertFeed({ agentSurface: null })).toEqual([])
  })

  it('treats an unreachable surface as a Classifier issue', () => {
    const feed = buildAlertFeed({
      agentSurface: { state: 'unreachable', backend: 'herdr' },
    })
    expect(feed).toHaveLength(1)
    expect(feed[0]?.kind).toBe('classifier')
    expect(feed[0]?.classifierState).toBe('down')
    expect(feed[0]?.backend).toBe('herdr')
    expect(feed[0]?.countsAsIssue).toBe(true)
  })
})

describe('countAlertIssues', () => {
  it('counts Failed + Degraded + down Classifier, not a healthy Classifier', () => {
    const feed = buildAlertFeed({
      plugins: [{ name: 'hugo', status: 'degraded', detail: 'not on PATH', activity: null }],
      runs: [
        { id: 1, project: 'p', task: 't', status: 'failed', started_at: '2026-09-01T00:00:00Z' },
        { id: 2, project: 'p', task: 't', status: 'failed', started_at: '2026-09-02T00:00:00Z' },
      ],
      agentSurface: { state: 'ok', backend: 'orca' },
    })
    expect(countAlertIssues(feed)).toBe(3)
  })

  it('returns 0 when the feed is empty or only a healthy classifier', () => {
    expect(countAlertIssues([])).toBe(0)
    expect(
      countAlertIssues(buildAlertFeed({ agentSurface: { state: 'ok', backend: 'orca' } })),
    ).toBe(0)
  })
})
