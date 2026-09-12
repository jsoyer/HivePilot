/**
 * Inbox ACTION / ROUTE confirm card (redesign PR4).
 *
 * The concierge classifier is fail-closed: POST /v1/concierge never
 * executes. ACTION / ROUTE / MULTI_ROUTE must show a confirm card; ANSWER
 * never does. Confirm is the only path that may call an existing run or
 * approval API. Cancel and ANSWER dispatch nothing.
 */

import { createRun, postApproval, type ConciergeDecision } from './pollen-api'

export type IntentKind = 'ACTION' | 'ROUTE'

export type ConciergeDispatchPlan =
  | { type: 'approval'; runId: number; approve: boolean }
  | { type: 'run'; task: string; project: string; extraPrompt?: string }
  | { type: 'none' }

export function requiresConfirmation(decision: ConciergeDecision): boolean {
  return decision.kind === 'action' || decision.kind === 'route' || decision.kind === 'multi_route'
}

export function intentKind(decision: ConciergeDecision): IntentKind {
  return decision.kind === 'action' ? 'ACTION' : 'ROUTE'
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function asRunId(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) return Number(value.trim())
  return null
}

export function planConciergeDispatch(decision: ConciergeDecision): ConciergeDispatchPlan {
  if (decision.kind === 'action' && (decision.action === 'approve' || decision.action === 'deny')) {
    const runId = asRunId(decision.params?.run_id)
    if (runId === null) return { type: 'none' }
    return { type: 'approval', runId, approve: decision.action === 'approve' }
  }
  if (decision.kind === 'action' && decision.action === 'run') {
    const task = asString(decision.params?.task)
    const project = asString(decision.target)
    if (!task || !project) return { type: 'none' }
    const extraPrompt = asString(decision.params?.order) ?? asString(decision.params?.extra_prompt) ?? undefined
    return { type: 'run', task, project, extraPrompt }
  }
  return { type: 'none' }
}

export async function dispatchConciergePlan(
  plan: ConciergeDispatchPlan,
  denyReason: string,
): Promise<string | null> {
  if (plan.type === 'approval') {
    await postApproval(plan.runId, {
      approve: plan.approve,
      reason: plan.approve ? undefined : denyReason,
    })
    return String(plan.runId)
  }
  if (plan.type === 'run') {
    const result = await createRun({
      task: plan.task,
      project: plan.project,
      extra_prompt: plan.extraPrompt,
      auto_git: true,
    })
    return String(result.run_id)
  }
  return null
}

export function describeIntentBody(
  decision: ConciergeDecision,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (decision.kind === 'multi_route') {
    const lines = (decision.dispatches ?? []).map((row) => {
      const project = row.target ? t('chat.intentProject', { project: row.target }) : ''
      const order = row.order ? ` — ${row.order}` : ''
      return `${row.role_key}${project ? ` · ${project}` : ''}${order}`
    })
    return lines.length > 0 ? lines.join('\n') : t('chat.intentFallback', { kind: intentKind(decision) })
  }

  if (decision.kind === 'route') {
    const project = decision.target ? t('chat.intentProject', { project: decision.target }) : ''
    const role = decision.role_key ?? t('chat.intentFallback', { kind: 'ROUTE' })
    const order = decision.order ? ` — ${decision.order}` : ''
    return `${role}${project ? ` · ${project}` : ''}${order}`
  }

  if (decision.action === 'approve' || decision.action === 'deny') {
    const runId = asRunId(decision.params?.run_id)
    if (runId !== null) {
      return t(decision.action === 'approve' ? 'chat.intentApproveRun' : 'chat.intentDenyRun', {
        id: String(runId),
      })
    }
  }

  if (decision.action === 'run_pipeline') {
    const pipeline = asString(decision.params?.pipeline) ?? t('chat.intentPipelineFallback')
    const project = decision.target ? t('chat.intentProject', { project: decision.target }) : ''
    const branch = asString(decision.params?.branch)
    const parts = [t('chat.intentLaunchPipeline', { pipeline })]
    if (project) parts.push(project)
    if (branch) parts.push(t('chat.intentBranch', { branch }))
    return parts.join(' · ')
  }

  if (decision.action === 'run') {
    const task = asString(decision.params?.task) ?? decision.action
    const project = decision.target ? t('chat.intentProject', { project: decision.target }) : ''
    return `${t('chat.intentLaunchTask', { task })}${project ? ` · ${project}` : ''}`
  }

  const label = decision.action ?? intentKind(decision)
  const target = decision.target ? ` · ${decision.target}` : ''
  return `${label}${target}`
}
