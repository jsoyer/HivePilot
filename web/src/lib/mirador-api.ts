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

/**
 * `succeeded / (succeeded + failed)` -- deliberately EXCLUDES `skipped`/
 * `other` from the denominator, unlike `OutcomeRates.succeeded` (which
 * divides by every run). `null` when there were zero attempts (e.g. a
 * group that's 100% skipped) -- never `0`, which would look identical to
 * "every attempt failed". See `hivepilot/services/analytics_service.py`'s
 * `_attempt_success_rate`.
 */
export type SuccessRate = number | null

export interface GroupOutcomeSummary {
  total: number
  outcomes: OutcomeCounts
  outcome_rates: OutcomeRates
  success_rate: SuccessRate
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
  success_rate: SuccessRate
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
  success_rate: SuccessRate
}

export interface ModelBreakdown {
  model: string
  total: number
  outcomes: OutcomeCounts
  outcome_rates: OutcomeRates
  success_rate: SuccessRate
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
  disabled: string[]
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

// ---------------------------------------------------------------------------
// GET /v1/runs, POST /v1/runs — Mirador actionable dashboard PRD, Sprint 3.
// Shapes transcribed from `hivepilot/services/state_service.py`'s `runs`
// table (`CREATE TABLE ... runs`, columns: id/project/task/status/detail/
// started_at/finished_at/tenant) and `hivepilot/services/api_service.py`'s
// `list_runs`/`NewRunRequest`/`create_run` — read those before changing
// anything here.
//
// `GET /v1/runs` requires a `run`-rank token (same gate `GET /v1/approvals`
// already uses) -- `fetchRuns` opts into `on403: 'forbidden'`.
// `POST /v1/runs` requires `run` too (`postJson` already defaults to
// `on403: 'forbidden'`) and responds 202 immediately -- the pipeline itself
// executes on a background thread server-side (see `create_run`'s
// docstring), so this call resolves fast regardless of how long the run
// takes; `RunsView` polls `GET /v1/runs` to observe status transitions.
//
// `detail` is untrusted free text (same caveat as `Approval.metadata` /
// `RunResult.detail` elsewhere in this app) — never render it.
// ---------------------------------------------------------------------------

export interface RunSummary {
  id: number
  project: string
  task: string
  status: string
  started_at: string
  finished_at?: string | null
  tenant?: string
  /** Untrusted free text (redacted server-side, but still opaque to the
   * UI's trust model) — never render this. */
  detail?: string | null
}

export function fetchRuns(): Promise<RunSummary[]> {
  return apiFetch<RunSummary[]>('/v1/runs', { on403: 'forbidden' })
}

export interface NewRunInput {
  task: string
  project: string
  extra_prompt?: string
  auto_git?: boolean
}

export interface NewRunResult {
  run_id: number
  status: string
}

export function createRun(body: NewRunInput): Promise<NewRunResult> {
  return postJson<NewRunResult>('/v1/runs', body)
}

// ---------------------------------------------------------------------------
// POST /v1/runs/{run_id}/cancel -- Mirador actionable dashboard PRD, Sprint 4.
// Shape transcribed from `hivepilot/services/api_service.py`'s
// `CancelRunResponse`/`cancel_run` -- read that before changing anything
// here. Requires a `run`-rank token (same gate `POST /v1/runs` already
// uses); `postJson` already defaults to `on403: 'forbidden'`. The endpoint
// responds 202 immediately (`status: 'cancelling'`) -- it only flips a
// cooperative flag, it does not wait for the run to actually stop; `RunsView`
// relies on its existing poll loop to observe the eventual `cancelled`
// status transition. A `409` (run not cancellable -- unknown/already-terminal
// run_id) surfaces as a thrown error from `apiFetch`, same as any other
// non-2xx response -- callers must handle it, never assume success.
// ---------------------------------------------------------------------------

export interface CancelRunResult {
  run_id: number
  status: string
}

export function cancelRun(runId: number): Promise<CancelRunResult> {
  return postJson<CancelRunResult>(`/v1/runs/${runId}/cancel`, {})
}

// ---------------------------------------------------------------------------
// POST /v1/plugins/{name}/toggle -- Mirador actionable dashboard PRD,
// Sprint 5. Shape transcribed from `hivepilot/services/api_service.py`'s
// `PluginToggleResponse`/`toggle_plugin_endpoint` -- read that before
// changing anything here. Admin-only (a stricter gate than every other
// action endpoint above, which require `run`/`approve`); `postJson` already
// defaults to `on403: 'forbidden'`, so a non-admin token calling this just
// throws `ApiForbiddenError` without being cleared.
//
// The change is effective on the API process's NEXT restart only -- plugins
// are scanned/registered once, at `Orchestrator()` construction time (see
// the backend endpoint's own docstring). `restart_required` is always
// `true` in the response; `HealthView` surfaces that explicitly so an admin
// never assumes the toggle took effect live.
// ---------------------------------------------------------------------------

export interface PluginToggleResult {
  name: string
  disabled: boolean
  restart_required: boolean
}

export function togglePlugin(name: string): Promise<PluginToggleResult> {
  return postJson<PluginToggleResult>(`/v1/plugins/${encodeURIComponent(name)}/toggle`, {})
}

// ---------------------------------------------------------------------------
// GET /v1/graph/sources, GET /v1/graph/{source}, GET /v1/graph/{source}/node/
// {node_id} — Mirador Graph View PRD, Sprint 3 web surface. Shapes
// transcribed directly from `hivepilot/graph.py`'s `GraphNode`/`GraphEdge`/
// `GraphData`/`GraphDetail`/`GraphSourceSpec` dataclasses and the
// `_graph_node_to_dict`/`_graph_edge_to_dict`/`_graph_data_to_dict`/
// `_graph_detail_to_dict`/`list_graph_sources_endpoint` JSON encoders in
// `hivepilot/services/api_service.py` — read those before changing anything
// here.
//
// A graph source's own `min_role` can be higher than the token gate's `read`
// floor check (exactly like `/v1/panels/{name}`'s `min_role` / `/v1/memories`'s
// `admin` gate) — `fetchGraph`/`fetchGraphNode` both opt into
// `on403: 'forbidden'` so a 403 throws `ApiForbiddenError` and leaves the
// token untouched, matching `fetchPanel`'s pattern. `fetchGraphSources`
// itself only requires the gate's own `read` floor (source metadata is
// configuration, not secret — mirrors `list_panels_endpoint`), so it uses
// the default `on403: 'clear'`.
//
// `GraphNode.meta` / `GraphDetail.sections` text content is source-authored
// and UNTRUSTED, exactly like `PanelData` — `GraphDetail.sections` reuses
// the closed `PanelSection` union above verbatim (see `graph.py`'s module
// docstring), so it renders through the EXISTING `PanelRenderer`, which
// already only ever uses plain JSX interpolation, never
// `dangerouslySetInnerHTML`.
// ---------------------------------------------------------------------------

export interface GraphNode {
  id: string
  label: string
  kind: string
  status: string | null
  group: string | null
  badges: string[]
  meta: Record<string, unknown>
}

export interface GraphEdge {
  source: string
  target: string
  kind: string | null
  label: string | null
}

export interface GraphData {
  source: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  layout_hint: string | null
}

export interface GraphSourceSummary {
  name: string
  title: string
  min_role: string
  params: string[]
}

export interface GraphSourcesResponse {
  sources: GraphSourceSummary[]
}

/** Every registered graph source's name/title/min_role/params — mirrors
 * `fetchPanels`. Role gate: `read` (the floor) — a source's own `min_role`
 * only gates fetching ITS data below (`fetchGraph`/`fetchGraphNode`), not
 * whether it's listed here. */
export function fetchGraphSources(): Promise<GraphSourcesResponse> {
  return apiFetch<GraphSourcesResponse>('/v1/graph/sources')
}

export interface GraphDetail {
  title: string
  tags: string[]
  sections: PanelSection[]
}

/** A single graph source's full node/edge data. `params` becomes the raw
 * query string (e.g. `{ pipeline: 'acme' }` -> `?pipeline=acme`) — the
 * backend's `GraphContext.params` is exactly `dict(request.query_params)`,
 * so this must stay a thin passthrough, never client-side filtering. Uses
 * `on403: 'forbidden'` — see module note above. */
export function fetchGraph(source: string, params?: Record<string, string>): Promise<GraphData> {
  const query = params && Object.keys(params).length > 0 ? `?${new URLSearchParams(params).toString()}` : ''
  return apiFetch<GraphData>(`/v1/graph/${encodeURIComponent(source)}${query}`, { on403: 'forbidden' })
}

/** A single node's detail view within *source* — `GraphDetail.sections`
 * renders via the existing `PanelRenderer`. Uses `on403: 'forbidden'` — see
 * module note above. A 404 (unknown source/node, or a source with no
 * `node_detail` callable) surfaces as a thrown `ApiError`, same as any
 * other non-2xx response — callers must handle it. */
export function fetchGraphNode(source: string, nodeId: string): Promise<GraphDetail> {
  return apiFetch<GraphDetail>(
    `/v1/graph/${encodeURIComponent(source)}/node/${encodeURIComponent(nodeId)}`,
    { on403: 'forbidden' },
  )
}

// ---------------------------------------------------------------------------
// GET /v1/memory/reality, /v1/memory/gaps, /v1/memory/evaluations,
// /v1/memory/journal — backs Mirador's "Réalité" memory-quality view. Shapes
// transcribed directly from `hivepilot/services/memory_service.py`'s
// `reality_summary`/`gaps_by_namespace`/`recent_evaluations`/
// `activity_journal` and `api_service.py`'s `memory_reality`/`memory_gaps`/
// `list_memory_evaluations`/`memory_journal` endpoints — read those before
// changing anything here.
//
// All four are `require_role("read")` server-side — the same floor the
// token gate itself already checks, so a token that passed the gate should
// never genuinely 403 here today. `on403: 'forbidden'` is still opted into
// (mirroring `fetchMemories`/`fetchPanel`/`fetchGraph` above) purely as
// defense-in-depth: if a future tenant-scoping change ever raises this
// floor, a 403 must not silently clear an otherwise-valid token out from
// under every other tab — `RealityView` peels `ApiForbiddenError` off per
// section, exactly like `GraphView`'s per-source handling.
//
// `namespace` / `query_or_key` / `note` / `actor` / `top_queries` are all
// caller-influenced free text (whatever a plugin passed to
// `memory_service.record_*`, e.g. `plugins/mem0.py`) — UNTRUSTED, same trust
// class as `PanelData`/`GraphDetail` above. `RealityView` renders every one
// of them via plain JSX interpolation only, never `dangerouslySetInnerHTML`.
// ---------------------------------------------------------------------------

export interface MemoryReality {
  search_success_rate: number
  total_searches: number
  no_result_count: number
  avg_freshness_seconds: number
  declared_reliability: number
  total_evaluations: number
}

export function fetchMemoryReality(days = 30): Promise<MemoryReality> {
  return apiFetch<MemoryReality>(`/v1/memory/reality?days=${days}`, { on403: 'forbidden' })
}

export interface MemoryGap {
  namespace: string
  no_result_count: number
  top_queries: string[]
}

export interface MemoryGapsResponse {
  gaps: MemoryGap[]
}

export function fetchMemoryGaps(days = 30): Promise<MemoryGapsResponse> {
  return apiFetch<MemoryGapsResponse>(`/v1/memory/gaps?days=${days}`, { on403: 'forbidden' })
}

export interface MemoryEvaluation {
  ts: string
  namespace: string
  ref_key: string | null
  useful: boolean | null
  note: string | null
  actor: string
}

export interface MemoryEvaluationsResponse {
  evaluations: MemoryEvaluation[]
}

export function fetchMemoryEvaluations(limit = 50): Promise<MemoryEvaluationsResponse> {
  return apiFetch<MemoryEvaluationsResponse>(`/v1/memory/evaluations?limit=${limit}`, { on403: 'forbidden' })
}

export interface MemoryJournalEntry {
  ts: string
  op: string
  namespace: string
  query_or_key: string | null
  result_count: number | null
  found: boolean | null
  freshness_seconds: number | null
  actor: string
}

export interface MemoryJournalResponse {
  journal: MemoryJournalEntry[]
}

export function fetchMemoryJournal(limit = 50): Promise<MemoryJournalResponse> {
  return apiFetch<MemoryJournalResponse>(`/v1/memory/journal?limit=${limit}`, { on403: 'forbidden' })
}

