import { describe, expect, it, vi } from 'vitest'
import type { ConciergeDecision } from './pollen-api'
import {
  describeIntentBody,
  dispatchConciergePlan,
  intentKind,
  planConciergeDispatch,
  requiresConfirmation,
} from './concierge-intent'

const { createRun, postApproval } = vi.hoisted(() => ({
  createRun: vi.fn(),
  postApproval: vi.fn(),
}))

vi.mock('./pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./pollen-api')>()
  return { ...actual, createRun, postApproval }
})

function decision(partial: Partial<ConciergeDecision>): ConciergeDecision {
  return {
    kind: 'answer',
    answer_text: null,
    role_key: null,
    target: null,
    order: null,
    action: null,
    params: null,
    destructive: false,
    dispatches: [],
    ...partial,
  }
}

const t = (key: string, params?: Record<string, string | number>) => {
  if (key === 'chat.intentProject') return `project ${params?.project}`
  if (key === 'chat.intentBranch') return `branch ${params?.branch}`
  if (key === 'chat.intentLaunchPipeline') return `Launch pipeline ${params?.pipeline}`
  if (key === 'chat.intentLaunchTask') return `Run ${params?.task}`
  if (key === 'chat.intentApproveRun') return `Approve run ${params?.id}`
  if (key === 'chat.intentDenyRun') return `Deny run ${params?.id}`
  if (key === 'chat.intentPipelineFallback') return 'pipeline'
  if (key === 'chat.intentFallback') return String(params?.kind ?? 'intent')
  return key
}

describe('requiresConfirmation', () => {
  it('is true only for ACTION / ROUTE / MULTI_ROUTE', () => {
    expect(requiresConfirmation(decision({ kind: 'answer' }))).toBe(false)
    expect(requiresConfirmation(decision({ kind: 'action', action: 'run_pipeline', destructive: true }))).toBe(
      true,
    )
    expect(requiresConfirmation(decision({ kind: 'route', role_key: 'developer', destructive: true }))).toBe(true)
    expect(requiresConfirmation(decision({ kind: 'multi_route', destructive: true }))).toBe(true)
  })
})

describe('intentKind', () => {
  it('labels action as ACTION and everything else requiring confirm as ROUTE', () => {
    expect(intentKind(decision({ kind: 'action', action: 'run' }))).toBe('ACTION')
    expect(intentKind(decision({ kind: 'route' }))).toBe('ROUTE')
    expect(intentKind(decision({ kind: 'multi_route' }))).toBe('ROUTE')
  })
})

describe('planConciergeDispatch', () => {
  it('plans approve/deny and run, and stays none for pipeline/route (no fabricated dispatch)', () => {
    expect(
      planConciergeDispatch(
        decision({ kind: 'action', action: 'approve', params: { run_id: 42 }, destructive: true }),
      ),
    ).toEqual({ type: 'approval', runId: 42, approve: true })
    expect(
      planConciergeDispatch(
        decision({
          kind: 'action',
          action: 'run',
          target: 'noxxy',
          params: { task: 'docs', order: 'add a healthcheck' },
          destructive: true,
        }),
      ),
    ).toEqual({ type: 'run', task: 'docs', project: 'noxxy', extraPrompt: 'add a healthcheck' })
    expect(
      planConciergeDispatch(
        decision({
          kind: 'action',
          action: 'run_pipeline',
          target: 'noxxy',
          params: { pipeline: 'noxys' },
          destructive: true,
        }),
      ),
    ).toEqual({ type: 'none' })
    expect(planConciergeDispatch(decision({ kind: 'route', role_key: 'developer', target: 'noxxy' }))).toEqual({
      type: 'none',
    })
  })
})

describe('describeIntentBody', () => {
  it('summarises a pipeline ACTION the way the confirm card shows it', () => {
    expect(
      describeIntentBody(
        decision({
          kind: 'action',
          action: 'run_pipeline',
          target: 'noxys',
          params: { pipeline: 'noxys', branch: 'staging' },
        }),
        t,
      ),
    ).toBe('Launch pipeline noxys · project noxys · branch staging')
  })
})

describe('dispatchConciergePlan', () => {
  it('does not call APIs for a none plan', async () => {
    await expect(dispatchConciergePlan({ type: 'none' }, 'Denied via Inbox')).resolves.toBeNull()
    expect(createRun).not.toHaveBeenCalled()
    expect(postApproval).not.toHaveBeenCalled()
  })
})
