import { describe, expect, it } from 'vitest'
import { countShellIssues } from './shell-issues'

function plugin(status: 'ok' | 'degraded' | 'error', name: string = status) {
  return { name, status, detail: '', activity: null }
}

function run(status: string, id = 1) {
  return { id, project: 'p', task: 't', status, started_at: '2026-09-01T00:00:00Z' }
}

describe('countShellIssues', () => {
  it('returns 0 when nothing is wrong', () => {
    expect(countShellIssues([plugin('ok', 'a'), plugin('ok', 'b')], [run('succeeded'), run('running', 2)])).toBe(
      0,
    )
  })

  it('counts failed runs and degraded/error plugins (mock A: 2 failed + 1 degraded)', () => {
    expect(
      countShellIssues(
        [plugin('ok', 'store'), plugin('degraded', 'headroom')],
        [run('failed', 1), run('failed', 2), run('succeeded', 3)],
      ),
    ).toBe(3)
  })

  it('does not count pending approvals-shaped statuses on runs', () => {
    expect(countShellIssues([], [run('waiting_approval')])).toBe(0)
  })

  it('counts plugin error as an issue', () => {
    expect(countShellIssues([plugin('error', 'obsidian')], [])).toBe(1)
  })

  it('counts a down classifier when extras are passed', () => {
    expect(
      countShellIssues([], [], { agentSurface: { state: 'unreachable', backend: 'herdr' } }),
    ).toBe(1)
  })

  it('does not count a healthy classifier', () => {
    expect(countShellIssues([], [], { agentSurface: { state: 'ok', backend: 'orca' } })).toBe(0)
  })
})
