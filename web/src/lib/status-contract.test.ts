import { describe, expect, it } from 'vitest'
import { attentionZone, needsAttention, runColumn } from './status-contract'

// This table MUST match `tests/test_status_contract.py` (the Python source of
// truth). If you change one, change both.
const COLUMN_CASES: Array<[string, string]> = [
  ['new', 'queued'],
  ['planned', 'queued'],
  ['pending', 'queued'],
  ['running', 'running'],
  ['approval', 'waitingApproval'],
  ['awaiting_approval', 'waitingApproval'],
  ['review', 'waitingApproval'],
  ['failed', 'failed'],
  ['denied', 'failed'],
  ['rate_limit', 'failed'],
  ['auth_expired', 'failed'],
  ['test_failure', 'failed'],
  ['security_blocker', 'failed'],
  ['success', 'done'],
  ['complete', 'done'],
  ['paused', 'other'],
  ['cancelled', 'other'],
  ['deferred', 'other'],
  ['totally_unknown', 'other'],
]

const ZONE_CASES: Array<[string, string]> = [
  ['running', 'working'],
  ['new', 'queued'],
  ['planned', 'queued'],
  ['approval', 'needs_you'],
  ['awaiting_approval', 'needs_you'],
  ['review', 'in_review'],
  ['failed', 'needs_you'],
  ['security_blocker', 'needs_you'],
  ['success', 'ready'],
  ['complete', 'ready'],
  ['paused', 'other'],
  ['cancelled', 'other'],
  ['deferred', 'other'],
]

describe('runColumn', () => {
  it.each(COLUMN_CASES)('maps %s -> column %s', (status, column) => {
    expect(runColumn(status)).toBe(column)
  })

  it('normalises case and whitespace', () => {
    expect(runColumn('  RUNNING ')).toBe('running')
  })
})

describe('attentionZone', () => {
  it.each(ZONE_CASES)('maps %s -> zone %s', (status, zone) => {
    expect(attentionZone(status)).toBe(zone)
  })

  it('normalises case and whitespace', () => {
    expect(attentionZone('Failed')).toBe('needs_you')
  })
})

describe('needsAttention', () => {
  it('is true only for failures and pending decisions', () => {
    expect(needsAttention('failed')).toBe(true)
    expect(needsAttention('approval')).toBe(true)
    expect(needsAttention('running')).toBe(false)
    expect(needsAttention('success')).toBe(false)
    expect(needsAttention('review')).toBe(false) // in_review, not needs_you
  })
})
