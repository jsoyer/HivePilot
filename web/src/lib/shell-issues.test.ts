import { describe, expect, it } from 'vitest'
import { countShellIssues } from './shell-issues'

describe('countShellIssues', () => {
  it('returns 0 when nothing is wrong', () => {
    expect(
      countShellIssues(
        [{ status: 'ok' }, { status: 'ok' }],
        [{ status: 'succeeded' }, { status: 'running' }],
      ),
    ).toBe(0)
  })

  it('counts failed runs and degraded/error plugins (mock A: 2 failed + 1 degraded)', () => {
    expect(
      countShellIssues(
        [{ status: 'ok' }, { status: 'degraded' }],
        [{ status: 'failed' }, { status: 'failed' }, { status: 'succeeded' }],
      ),
    ).toBe(3)
  })

  it('does not count pending approvals-shaped statuses on runs', () => {
    expect(countShellIssues([], [{ status: 'waiting_approval' }])).toBe(0)
  })

  it('counts plugin error as an issue', () => {
    expect(countShellIssues([{ status: 'error' }], [])).toBe(1)
  })
})
