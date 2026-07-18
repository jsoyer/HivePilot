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

// ---------------------------------------------------------------------------
// GET /v1/panels, GET /v1/panels/{name} — Mirador plugin `panel` type
// (Sprint 3 web surface). Shapes transcribed from `hivepilot/plugins.py`
// `PanelSpec` / `PanelData` / `PanelStatSection` / `PanelTableSection` /
// `PanelTextSection` — read that file before changing anything here.
// Section content (label/value/content/table cells) is plugin-authored and
// UNTRUSTED (see `PanelData`'s docstring there): `PanelRenderer` renders it
// via plain JSX interpolation only, never `dangerouslySetInnerHTML`.
// ---------------------------------------------------------------------------

export interface PanelSummary {
  name: string
  title: string
  min_role: string
}

export interface PanelsResponse {
  panels: PanelSummary[]
}

export interface PanelStatSection {
  kind: 'stat'
  label: string
  value: string
  status: 'ok' | 'warn' | 'error' | null
}

export interface PanelTableSection {
  kind: 'table'
  columns: string[]
  rows: string[][]
}

export interface PanelTextSection {
  kind: 'text'
  content: string
}

export type PanelSection = PanelStatSection | PanelTableSection | PanelTextSection

export interface PanelData {
  sections: PanelSection[]
}

/** Every registered panel's name/title/min_role. Role gate: `read` (the
 * floor) — a panel's own `min_role` only gates fetching ITS data below, not
 * whether it's listed here. */
export function fetchPanels(): Promise<PanelsResponse> {
  return apiFetch<PanelsResponse>('/v1/panels')
}

/** A single panel's data. A panel's own `min_role` may be higher than the
 * token gate's floor check (exactly like `/v1/memories`'s `admin` gate) —
 * uses `on403: 'forbidden'` so an under-role token throws
 * `ApiForbiddenError` without being cleared. See `PanelView`. */
export function fetchPanel(name: string): Promise<PanelData> {
  return apiFetch<PanelData>(`/v1/panels/${encodeURIComponent(name)}`, { on403: 'forbidden' })
}

// ---------------------------------------------------------------------------
// GET /v1/whoami — Mirador actionable dashboard PRD, Sprint 1. Resolves the
// calling token's own role/tenant; backs `useRole()` (`@/lib/role-context`),
// which fail-closed gates action controls app-wide (unknown/null role ->
// `can()` false for everything). Ranked the same as the backend's
// `ROLE_RANKS` (`hivepilot/services/token_service.py`): read < run <
// approve < admin.
// ---------------------------------------------------------------------------

export type Role = 'read' | 'run' | 'approve' | 'admin'

export interface WhoAmI {
  role: Role
  tenant: string
}

export function whoami(): Promise<WhoAmI> {
  return apiFetch<WhoAmI>('/v1/whoami')
}

// ---------------------------------------------------------------------------
// postJson — generic POST helper for downstream sprints (S2-S5: approve/
// deny actions, async run triggers, plugin toggles, ...). Every action
// endpoint those sprints add requires a role STRICTLY higher than the token
// gate's own `read` floor check, so — like `fetchMemories`/`fetchPanel`
// above — this always opts into `on403: 'forbidden'`: a 403 here means the
// token is valid but under-privileged for this one action, not that the
// token itself should be cleared (see `ApiForbiddenError` in `./api`).
// ---------------------------------------------------------------------------

export function postJson<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    on403: 'forbidden',
  })
}

// ---------------------------------------------------------------------------
// GET /v1/approvals, POST /v1/approvals/{run_id} — Mirador actionable
// dashboard PRD, Sprint 2. Shapes transcribed from `hivepilot/services/
// state_service.py`'s `approvals` table (`CREATE TABLE ... approvals`,
// columns: run_id/project/task/metadata/status/requested_at/approved_by/
// approved_at/tenant) and `hivepilot/services/api_service.py`'s
// `pending_approvals` / `ApprovalAction` / `handle_approval` — read those
// before changing anything here.
//
// Role gates (from `api_service.py`, NOT just the token gate's `read`
// floor): `GET /v1/approvals` requires `run`, so a plain `read` token 403s
// fetching the list itself — `fetchApprovals` therefore opts into
// `on403: 'forbidden'` exactly like `fetchMemories`/`fetchPanel` above, and
// `ApprovalsView` special-cases that error into a graceful message instead
// of clearing the token. `POST /v1/approvals/{run_id}` requires `approve`
// (`postJson` already defaults to `on403: 'forbidden'`).
//
// `metadata` is a raw JSON-TEXT column populated by whatever pipeline stage
// requested the approval (see `orchestrator.py`'s `record_approval_request`
// call sites — it can carry an `extra_prompt`/`planning_context` excerpt,
// i.e. untrusted free text, the same class of field as `RunResult.detail`/
// `capture()` output elsewhere in this app). `ApprovalsView` must NEVER
// render it — only the typed, structural fields below.
// ---------------------------------------------------------------------------

export interface Approval {
  run_id: number
  project: string
  task: string
  status: string
  requested_at: string
  tenant?: string
  approved_by?: string | null
  approved_at?: string | null
  /** Raw JSON text — untrusted, opaque to the UI. Never render this. */
  metadata?: string
}

export function fetchApprovals(): Promise<Approval[]> {
  return apiFetch<Approval[]>('/v1/approvals', { on403: 'forbidden' })
}

export interface ApprovalActionInput {
  approve: boolean
  reason?: string
}

/** POST body sent to `/v1/approvals/{run_id}` — matches `ApprovalAction` in
 * `api_service.py` (`approver: str = "api"`, `approve: bool = True`,
 * `reason: str | None = None`). The web UI always identifies itself as
 * `approver: "web"`. */
export interface ApprovalActionBody {
  approver: 'web'
  approve: boolean
  reason?: string
}

/** The endpoint responds `{"result": <RunResult.__dict__>}` — `RunResult`
 * carries a `detail` field that is untrusted/unredacted free text (same
 * caveat as `Approval.metadata` above); this type only surfaces `success`,
 * which is all `ApprovalsView` needs (it never renders `detail`). */
export interface ApprovalActionResult {
  result: {
    success: boolean
    skipped?: boolean
  }
}

export function postApproval(runId: number, action: ApprovalActionInput): Promise<ApprovalActionResult> {
  const body: ApprovalActionBody = { approver: 'web', approve: action.approve, reason: action.reason }
  return postJson<ApprovalActionResult>(`/v1/approvals/${runId}`, body)
}
