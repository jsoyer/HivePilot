/**
 * Typed shapes + fetch wrappers for the Mirador web UI's data sources —
 * HivePilot's own `/v1/analytics/*`, `/v1/plugins/health`, and `/v1/memories`
 * endpoints. Field names/shapes are transcribed directly from
 * `hivepilot/services/analytics_service.py` and `hivepilot/services/
 * api_service.py` (read those before changing anything here — this file
 * must never guess at a response shape).
 *
 * Every wrapper is a thin `apiFetch<T>(path)` call — no client-side
 * aggregation or re-derivation of numbers the API already computed.
 */

import { apiFetch } from './api'

// ---------------------------------------------------------------------------
// Shared shapes
// ---------------------------------------------------------------------------

export interface OutcomeCounts {
  succeeded: number
  failed: number
  skipped: number
  other: number
}

export type OutcomeRates = OutcomeCounts

export interface GroupOutcomeSummary {
  total: number
  outcomes: OutcomeCounts
  outcome_rates: OutcomeRates
}

export interface DurationStats {
  count: number
  min: number
  max: number
  avg: number
  p50: number
  p95: number
  p99: number
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/summary
// ---------------------------------------------------------------------------

export interface AnalyticsSummary {
  total: number
  outcomes: OutcomeCounts
  outcome_rates: OutcomeRates
  by_project: Record<string, GroupOutcomeSummary>
  by_task: Record<string, GroupOutcomeSummary>
  by_raw_status: Record<string, number>
}

export function fetchAnalyticsSummary(days = 30): Promise<AnalyticsSummary> {
  return apiFetch<AnalyticsSummary>(`/v1/analytics/summary?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/trends
// ---------------------------------------------------------------------------

export interface TrendPoint {
  bucket: string
  total: number
  outcomes: OutcomeCounts
}

export interface AnalyticsTrends {
  bucket: 'day' | 'week'
  series: TrendPoint[]
}

export function fetchAnalyticsTrends(days = 30, bucket: 'day' | 'week' = 'day'): Promise<AnalyticsTrends> {
  return apiFetch<AnalyticsTrends>(`/v1/analytics/trends?days=${days}&bucket=${bucket}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/durations
// ---------------------------------------------------------------------------

export interface AnalyticsDurations {
  overall: DurationStats
  by_project: Record<string, DurationStats>
  by_task: Record<string, DurationStats>
}

export function fetchAnalyticsDurations(days = 30): Promise<AnalyticsDurations> {
  return apiFetch<AnalyticsDurations>(`/v1/analytics/durations?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/steps/failures
// ---------------------------------------------------------------------------

export interface StepFailureHotspot {
  step: string
  status: string
  count: number
}

export interface StepFailuresResponse {
  hotspots: StepFailureHotspot[]
}

export function fetchStepFailures(days = 30, limit = 20): Promise<StepFailuresResponse> {
  return apiFetch<StepFailuresResponse>(`/v1/analytics/steps/failures?days=${days}&limit=${limit}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/approvals/latency
// ---------------------------------------------------------------------------

/** Same shape as `DurationStats` — the endpoint returns `_duration_stats(...)`
 * directly (not wrapped in an envelope). */
export type ApprovalLatency = DurationStats

export function fetchApprovalLatency(days = 30): Promise<ApprovalLatency> {
  return apiFetch<ApprovalLatency>(`/v1/analytics/approvals/latency?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/providers
// ---------------------------------------------------------------------------

export interface ProviderBreakdown {
  provider: string
  total: number
  outcomes: OutcomeCounts
  outcome_rates: OutcomeRates
}

export interface ModelBreakdown {
  model: string
  total: number
  outcomes: OutcomeCounts
  outcome_rates: OutcomeRates
}

export interface AnalyticsProviders {
  by_provider: ProviderBreakdown[]
  by_model: ModelBreakdown[]
}

export function fetchAnalyticsProviders(days = 30): Promise<AnalyticsProviders> {
  return apiFetch<AnalyticsProviders>(`/v1/analytics/providers?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/cost
// ---------------------------------------------------------------------------

export interface CostAccumulation {
  total_steps: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  unpriced_steps: number
}

export interface ProviderCost extends CostAccumulation {
  provider: string
}

export interface ModelCost extends CostAccumulation {
  model: string
}

export interface AnalyticsCost {
  overall: CostAccumulation
  by_provider: ProviderCost[]
  by_model: ModelCost[]
}

export function fetchAnalyticsCost(days = 30): Promise<AnalyticsCost> {
  return apiFetch<AnalyticsCost>(`/v1/analytics/cost?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/plugins/health
// ---------------------------------------------------------------------------

export type PluginHealthStatus = 'ok' | 'degraded' | 'error'

export interface PluginHealthEntry {
  name: string
  status: PluginHealthStatus
  detail: string
}

export interface PluginsHealthResponse {
  plugins: PluginHealthEntry[]
}

export function fetchPluginsHealth(): Promise<PluginsHealthResponse> {
  return apiFetch<PluginsHealthResponse>('/v1/plugins/health')
}

// ---------------------------------------------------------------------------
// GET /v1/memories — admin-only (see api_service.py `list_memories`
// docstring for the full scope/tenant analysis). Uses `on403: 'forbidden'`
// so a valid non-admin token isn't cleared just because this one endpoint is
// out of its reach — see `ApiForbiddenError` in `./api`.
// ---------------------------------------------------------------------------

export interface MemoryProvenance {
  project?: string
  task?: string
  role?: string
  category?: string
  ts?: string
  [key: string]: unknown
}

export interface MemoryItem {
  memory: string
  id?: string | number
  metadata?: MemoryProvenance
  score?: number
}

export interface MemoriesResponse {
  configured: boolean
  memories: MemoryItem[]
  detail?: string
}

export function fetchMemories(query: string, limit = 20): Promise<MemoriesResponse> {
  const params = new URLSearchParams({ query, limit: String(limit) })
  return apiFetch<MemoriesResponse>(`/v1/memories?${params.toString()}`, { on403: 'forbidden' })
}
