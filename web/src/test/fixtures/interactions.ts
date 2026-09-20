import type { ConversationMessage, ConversationThread } from '@/lib/pollen-api'

/**
 * A realistic actor→target fil for one pipeline run.
 *
 * Mirrors how `Orchestrator.run_pipeline` records stage outputs:
 * `actor` is the display label, `target` is the next stage's agent,
 * and `role` is the machine key from `interactions.metadata.role`.
 */
export const INTERACTION_MESSAGES: ConversationMessage[] = [
  {
    interaction_id: 101,
    actor: 'Aliénor (CEO)',
    role: 'ceo',
    action: 'completed stage',
    target: 'Gustave (Developer)',
    body: 'Objective: ship the metrics table.\nKeep the grant path behind isAdmin.',
    at: '2026-08-13T20:05:40',
  },
  {
    interaction_id: 102,
    actor: 'Gustave (Developer)',
    role: 'developer',
    action: 'completed stage',
    target: 'Victor (Reviewer)',
    body: 'Implemented mdstat with 38 tests.',
    at: '2026-08-13T20:06:00',
  },
  {
    interaction_id: 103,
    actor: 'Victor (Reviewer)',
    role: 'reviewer',
    action: 'completed stage',
    target: null,
    body: 'status: REQUEST_CHANGES\nThe grant path never checks isAdmin.',
    at: '2026-08-13T20:09:00',
  },
]

export const INTERACTION_THREAD: ConversationThread = {
  run_id: 42,
  roles: ['ceo', 'developer', 'reviewer'],
  messages: INTERACTION_MESSAGES,
}
