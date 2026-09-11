/**
 * HP-64 typed aliases over the generated OpenAPI contract.
 * Paths here must stay in lockstep with `web/openapi.json`.
 */
import type { components, paths } from './openapi'

export type ContractPaths = keyof paths
export type RolesGet = paths['/v1/roles']['get']
export type RoleByNameGet = paths['/v1/roles/{name}']['get']
export type RoleDraftPost = paths['/v1/roles/draft']['post']
export type ConciergePost = paths['/v1/concierge']['post']
export type SchedulesGet = paths['/v1/schedules']['get']
export type ScheduleTriggerPost = paths['/v1/webhook/trigger/{schedule_name}']['post']

export type RoleListResponse = components['schemas']['RoleListResponse']
export type ConciergeDecisionOut = components['schemas']['ConciergeDecisionOut']
export type ScheduleListResponse = components['schemas']['ScheduleListResponse']
export type ScheduleOut = components['schemas']['ScheduleOut']
export type TriggerResponse = components['schemas']['TriggerResponse']
