import { describe, expect, it } from 'vitest'
import type { ContractPaths } from './contract'

const CONTRACT: ContractPaths[] = [
  '/v1/roles',
  '/v1/roles/{name}',
  '/v1/concierge',
  '/v1/schedules',
  '/v1/webhook/trigger/{schedule_name}',
]

describe('HP-64 OpenAPI contract', () => {
  it('exposes roles, concierge, and schedules', () => {
    expect(new Set(CONTRACT).size).toBe(5)
  })
})
