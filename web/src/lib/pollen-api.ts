/**
 * Typed shapes + fetch wrappers for the Pollen web UI's data sources —
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

export interface ProjectCost extends CostAccumulation {
  project: string
}

/**
 * `by_project`/`unpriced_models` (Mirador Home command-center sprint) —
 * transcribed from `cost_summary`'s return value, same as `by_provider`/
 * `by_model` above. `by_role` is always `null` today — `cost_summary`'s
 * `_BY_ROLE_UNAVAILABLE_NOTE` docstring explains why (no role column on
 * steps/runs) — `by_role_note` carries that same explanation, never a
 * fabricated breakdown.
 */
export interface AnalyticsCost {
  overall: CostAccumulation
  by_provider: ProviderCost[]
  by_model: ModelCost[]
  by_project: ProjectCost[]
  by_role: null
  by_role_note: string
  unpriced_models: string[]
}

export function fetchAnalyticsCost(days = 30): Promise<AnalyticsCost> {
  return apiFetch<AnalyticsCost>(`/v1/analytics/cost?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/models — Mirador Home command-center sprint. Shape transcribed
// from `hivepilot/services/analytics_service.py`'s `models_summary` — read
// that before changing anything here. `latency_available` is always `false`
// today: p50/p95 latency isn't computable from current data (see
// `models_summary`'s own `_LATENCY_UNAVAILABLE_NOTE`, paired with
// `latency_note` here) — intentionally omitted rather than fabricated.
// ---------------------------------------------------------------------------

export interface ModelRollup {
  model: string
  step_count: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  unpriced_steps: number
  success_rate: SuccessRate
  share_of_spend: number
}

export interface ModelsOverall extends CostAccumulation {
  succeeded_runs: number
  /** `null` when there are zero succeeded runs in-window — never a
   * misleading `0`, mirrors `SuccessRate`'s own "no attempts" contract. */
  cost_per_successful_run: number | null
}

export interface ModelsSummary {
  models: ModelRollup[]
  overall: ModelsOverall
  latency_available: boolean
  latency_note: string
}

export function fetchModels(days = 30, project?: string, task?: string): Promise<ModelsSummary> {
  const params = new URLSearchParams({ days: String(days) })
  if (project) params.set('project', project)
  if (task) params.set('task', task)
  return apiFetch<ModelsSummary>(`/v1/models?${params.toString()}`)
}

// ---------------------------------------------------------------------------
// GET /v1/efficiency — Mirador Home command-center sprint. Shape transcribed
// from `hivepilot/services/efficiency_service.py`'s `efficiency_summary` —
// read that (and `headroom_metrics.efficiency_summary`) before changing
// anything here. `headroom` is real and NEVER `null` (zero-safe when
// nothing has been recorded yet); `rtk` is best-effort GLOBAL telemetry (not
// tenant-scoped — see that module's docstring) and is `null` whenever the
// `rtk` binary is absent/erroring/unparseable — never a fabricated number.
// ---------------------------------------------------------------------------

export interface HeadroomEfficiency {
  total_compressions: number
  chars_saved: number
  avg_ratio: number
  p95_ratio: number
  est_tokens_saved: number
}

export interface RtkSavedPoint {
  date: string
  saved_tokens: number
}

export interface RtkEfficiency {
  gain_pct: number
  tokens_saved: number
  total_commands: number
  saved_series: RtkSavedPoint[]
  /** Always `null` — `rtk gain -f json` has no per-command breakdown, only
   * its text/`-H` output does; never scraped/fabricated from that. */
  top_commands: null
}

export interface EfficiencySummary {
  headroom: HeadroomEfficiency
  rtk: RtkEfficiency | null
}

export function fetchEfficiency(days = 30): Promise<EfficiencySummary> {
  return apiFetch<EfficiencySummary>(`/v1/efficiency?days=${days}`)
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
// GET /v1/panels, GET /v1/panels/{name} — Pollen plugin `panel` type
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
// GET /v1/approvals, POST /v1/approvals/{run_id} — Pollen actionable
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
// GET /v1/projects, GET /v1/tasks — the CATALOGUE endpoints. Both gate at
// `read` (see `list_projects`/`list_tasks` in `api_service.py`), so any token
// that can open Pollen at all can enumerate them.
//
// Why they exist here: `POST /v1/runs` takes a `project` and a `task` that
// must both already be declared in config. The web UI used to ask an
// operator to TYPE them into free-text boxes with `e.g. deploy` hints, which
// is a guessing game with a 4xx at the end of it. These two fetchers turn
// both fields into a pick-list of the values the server actually accepts.
//
// Response shapes are deliberately parsed DEFENSIVELY:
//   - `/v1/tasks` returns `list(tasks.keys())` — a JSON array of names.
//   - `/v1/projects` returns the `projects` MAPPING (name -> project config),
//     whose values carry config detail this module has no business typing.
// `toNameList` accepts either shape and yields sorted names, so a backend
// that later switches one for the other cannot break the selector. Anything
// else (null, a number, a nested object) yields an EMPTY list, and every
// caller treats "empty catalogue" as "fall back to a free-text field" rather
// than "lock the operator out of creating a run".
// ---------------------------------------------------------------------------

/** Normalises `string[]` or `Record<string, unknown>` into sorted names.
 * Non-string array entries are dropped, not coerced — a name we cannot
 * trust is worse than a name we do not offer. */
export function toNameList(payload: unknown): string[] {
  const names = Array.isArray(payload)
    ? payload.filter((entry): entry is string => typeof entry === 'string')
    : payload !== null && typeof payload === 'object'
      ? Object.keys(payload as Record<string, unknown>)
      : []
  return [...new Set(names.map((name) => name.trim()).filter((name) => name.length > 0))].sort((a, b) =>
    a.localeCompare(b),
  )
}

export async function fetchProjectNames(): Promise<string[]> {
  return toNameList(await apiFetch<unknown>('/v1/projects', { on403: 'forbidden' }))
}

export async function fetchTaskNames(): Promise<string[]> {
  return toNameList(await apiFetch<unknown>('/v1/tasks', { on403: 'forbidden' }))
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
// GET /v1/runs/{run_id} -- Mirador Operate section PRD (Run Board + run
// detail drill-down). Shape transcribed from `hivepilot/services/
// api_service.py`'s `RunStepDetail`/`RunDetailResponse`/`get_run` -- read
// those before changing anything here. Phase 14b's "async family's missing
// piece" -- how `RunDetailPanel` polls a single run's full step timeline by
// id (provider/model/token/cost per step), which `GET /v1/runs` (the list
// `fetchRuns` above uses) never exposes.
//
// Gated at `run`, same as `GET /v1/runs`/`POST /v1/runs`/`POST /v1/runs/
// {run_id}/cancel` -- `fetchRun` opts into `on403: 'forbidden'` exactly like
// `fetchRuns`. A `404` (unknown run_id, OR a cross-tenant id a non-admin
// caller must never be able to distinguish from "doesn't exist" -- see the
// backend docstring) surfaces as a thrown `ApiError`; callers must handle it
// like any other non-2xx response, never assume success.
//
// `detail`/step `detail` are returned exactly as persisted -- already
// redacted server-side (see `get_run`'s own docstring) -- so, unlike
// `RunSummary.detail`/`Approval.metadata` elsewhere in this app, these ARE
// safe to render, but ONLY as plain JSX text (React's auto-escaping), never
// `dangerouslySetInnerHTML` -- `RunDetailPanel` must never treat them as
// trusted markup.
// ---------------------------------------------------------------------------

export interface RunStepDetail {
  step: string
  status: string
  detail?: string | null
  provider?: string | null
  model?: string | null
  input_tokens?: number | null
  output_tokens?: number | null
  cost_usd?: number | null
  timestamp?: string | null
}

export interface RunDetail {
  run_id: number
  project: string
  task: string
  status: string
  detail?: string | null
  started_at?: string | null
  finished_at?: string | null
  tenant?: string
  steps: RunStepDetail[]
}

export function fetchRun(runId: number): Promise<RunDetail> {
  return apiFetch<RunDetail>(`/v1/runs/${runId}`, { on403: 'forbidden' })
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
  /** Pollen graph-cascade rebuild: a generic, arbitrary, source-authored
   * extensibility hook (mirrors `GraphNode.meta`) — see `hivepilot/graph.py`'s
   * `GraphData` docstring. The built-in `pipeline` source uses it to expose
   * a run selector: `{ runs: GraphRunOption[], selected_run_id: number |
   * null, live: boolean }` — see `parseGraphRunSelector` below, which reads
   * this defensively (never trusts the shape blindly; it's server-authored
   * but arbitrary per the closed contract's own design). A source that
   * doesn't populate it leaves this at `{}`. */
  meta: Record<string, unknown>
}

/** One entry of the `pipeline` source's `GraphData.meta.runs` — a recent
 * run summary for the run-selector dropdown. Deliberately narrower than
 * `RunSummary` (`GET /v1/runs`, gated at the higher `run` role) — this
 * travels over the `read`-gated `/v1/graph/*` endpoints, so it carries only
 * id/started_at/status, never `detail` (untrusted free text) or tenant. */
export interface GraphRunOption {
  id: number
  started_at: string | null
  status: string | null
}

/** Defensively parses `GraphData.meta` into the `pipeline` source's run-
 * selector shape, or `null` when *meta* doesn't look like that shape at all
 * (a different source, or a `pipeline` response predating this field) —
 * `GraphView` renders no run selector at all in that case, never a crash
 * from blindly trusting arbitrary source-authored `meta`. */
export function parseGraphRunSelector(
  meta: Record<string, unknown>,
): { runs: GraphRunOption[]; selectedRunId: number | null; live: boolean } | null {
  const rawRuns = meta.runs
  if (!Array.isArray(rawRuns)) return null
  const runs: GraphRunOption[] = []
  for (const entry of rawRuns) {
    if (
      entry &&
      typeof entry === 'object' &&
      typeof (entry as Record<string, unknown>).id === 'number'
    ) {
      const record = entry as Record<string, unknown>
      runs.push({
        id: record.id as number,
        started_at: typeof record.started_at === 'string' ? record.started_at : null,
        status: typeof record.status === 'string' ? record.status : null,
      })
    }
  }
  const selectedRunId = typeof meta.selected_run_id === 'number' ? meta.selected_run_id : null
  const live = meta.live === true
  return { runs, selectedRunId, live }
}

export interface GraphSourceSummary {
  name: string
  title: string
  min_role: string
  params: string[]
  /** Enumerable values for some of `params`, when the source can list them
   * (see `GraphSourceSpec.param_options` server-side). A HINT for rendering
   * a pick-list instead of a free-text box — never a constraint, and a param
   * may legitimately be absent here. Optional on the wire so an older
   * backend, or a hand-rolled fixture, still parses. */
  param_options?: Record<string, string[]>
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
// /v1/memory/journal — backs Pollen's "Memory > Quality" memory-quality view. Shapes
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
// under every other tab — `MemoryQualityView` peels `ApiForbiddenError` off per
// section, exactly like `GraphView`'s per-source handling.
//
// `namespace` / `query_or_key` / `note` / `actor` / `top_queries` are all
// caller-influenced free text (whatever a plugin passed to
// `memory_service.record_*`, e.g. `plugins/mem0.py`) — UNTRUSTED, same trust
// class as `PanelData`/`GraphDetail` above. `MemoryQualityView` renders every one
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

// ---------------------------------------------------------------------------
// GET /v1/memory/growth — Mirador Home command-center sprint. Shape
// transcribed from `hivepilot/services/memory_service.py`'s
// `growth_summary` — read that before changing anything here. `authorship`
// is always `null` (a true human-vs-agent split isn't tracked — see that
// function's docstring); `by_actor` is the real breakdown that IS available
// instead of a fabricated one. Same `on403: 'forbidden'` opt-in as every
// other `/v1/memory/*` fetcher above.
// ---------------------------------------------------------------------------

export interface MemoryGrowthNamespace {
  namespace: string
  count: number
}

export interface MemoryGrowthActor {
  actor: string
  count: number
}

export interface MemoryGrowthPoint {
  date: string
  created: number
}

export interface MemoryGrowth {
  total: number
  memories_by_namespace: MemoryGrowthNamespace[]
  growth_series: MemoryGrowthPoint[]
  authorship: null
  by_actor: MemoryGrowthActor[]
  source: string
}

export function fetchMemoryGrowth(days = 30): Promise<MemoryGrowth> {
  return apiFetch<MemoryGrowth>(`/v1/memory/growth?days=${days}`, { on403: 'forbidden' })
}

// ---------------------------------------------------------------------------
// GET /v1/autopilot, POST /v1/autopilot/pause, POST /v1/autopilot/resume —
// Mirador Autopilot view. Shapes transcribed from `hivepilot/services/
// api_service.py`'s `AutopilotStateResponse`/`AutopilotControlResponse` (read
// the large module comment above `_resolve_autopilot_tenant` there — the
// full real-vs-null / tenant-lock contract — before changing anything here).
//
// `GET /v1/autopilot` is gated at the `read` floor (unlike `/v1/runs` or
// `/v1/approvals`, which require `run`) — a valid token normally never 403s
// fetching this state; the one exception is a non-admin caller whose token
// tenant disagrees with an explicit `?tenant=` (`_resolve_autopilot_tenant`),
// which this client never passes — so `on403: 'forbidden'` here is a
// defensive opt-in for that edge case (and any future tightening of the
// gate), same posture as `fetchApprovals`/`fetchRuns` above, so
// `AutopilotView` can render a graceful message instead of clearing a valid
// token.
//
// `POST /v1/autopilot/pause|resume` are gated at `run` (a control action,
// like `POST /v1/runs/{id}/cancel`) — `postJson` already opts into
// `on403: 'forbidden'`.
//
// `budget_daily_usd`/`budget_spent_today`/`budget_remaining` follow the
// backend's real-or-honest-null contract: `budget_daily_usd` is `null` iff
// no positive daily budget is configured; `budget_spent_today`/
// `budget_remaining` are `null` iff the spend lookup itself failed — never
// fabricated as `0`/full-budget. `AutopilotView` renders "unknown" for a
// `null` spend/remaining, never `0`/the full budget.
// ---------------------------------------------------------------------------

export interface AutopilotQueueItem {
  id: number
  pipeline: string
  project: string
  reason: string | null
  state: string
  enqueued_at: string
}

export interface AutopilotDispatch {
  pipeline: string
  project: string
  outcome: string
  at: string
}

export interface AutopilotState {
  tenant: string
  paused: boolean
  queue: AutopilotQueueItem[]
  queue_depth: number
  budget_daily_usd: number | null
  budget_spent_today: number | null
  budget_remaining: number | null
  recent_dispatches: AutopilotDispatch[]
  auto_dispatch_allowlist: string[]
}

export function fetchAutopilot(): Promise<AutopilotState> {
  return apiFetch<AutopilotState>('/v1/autopilot', { on403: 'forbidden' })
}

export interface AutopilotControlResult {
  tenant: string
  paused: boolean
}

export function pauseAutopilot(): Promise<AutopilotControlResult> {
  return postJson<AutopilotControlResult>('/v1/autopilot/pause', {})
}

export function resumeAutopilot(): Promise<AutopilotControlResult> {
  return postJson<AutopilotControlResult>('/v1/autopilot/resume', {})
}

// ---------------------------------------------------------------------------
// GET /v1/agents, GET /v1/lessons, GET /v1/verdicts — Mirador "Agents" view
// (frontend for the Mirador Agent Panels backend sprint). Shapes transcribed
// directly from `hivepilot/services/analytics_service.py`'s `agents_summary`/
// `lessons_summary`/`verdicts_summary` and `state_service.py`'s
// `list_lessons_by_tenant`/`list_verdicts` — read those before changing
// anything here.
//
// All three are `require_role("read")` server-side — the same floor the
// token gate itself already checks, so a valid token should never genuinely
// 403 here today. All three still opt into `on403: 'forbidden'` — same
// defense-in-depth posture as the `/v1/memory/*` fetchers above (see the
// module comment above `fetchMemoryReality`): if a future tenant-scoping
// change ever raises the floor, a 403 must not silently clear an otherwise-
// valid token out from under every other tab.
//
// Honesty contract (see `agents_summary`'s own docstring / `_AGENTS_
// ATTRIBUTION_NOTE`): a roster role with zero attributed steps comes back
// `attributed: false`, all-zero counts, `success_rate: null` — never a
// fabricated rollup. `NULL`-role (pre-migration/unattributed) activity is
// NEVER folded into any named role — it's the separate top-level `unknown`
// bucket. No latency figure is ever included (same gap as `/v1/models`).
// `AgentsView` renders `note` verbatim rather than re-explaining it.
// ---------------------------------------------------------------------------

/** Per-role activity counters shared by both a named roster entry
 * (`AgentRoster`) and the top-level `unknown` (NULL-role) bucket. */
export interface AgentActivityStats {
  run_count: number
  step_count: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  unpriced_steps: number
  success_rate: SuccessRate
  last_active: string | null
}

export interface AgentRoster extends AgentActivityStats {
  name: string
  /** `null` for a role name observed in the data but no longer present in
   * the current roster (`roles.yaml` changed) — surfaced honestly rather
   * than silently dropped, per `agents_summary`'s docstring. */
  display_name: string | null
  title: string | null
  /** `false` means zero attributed steps in-window — "no data yet", never a
   * fabricated rollup. See `success_rate`'s own `null` contract alongside
   * it (both flip together). */
  attributed: boolean
}

/** The NULL-role ("unknown") bucket — same counters as a roster entry,
 * minus `attributed` (always true by construction: it's whatever activity
 * actually has no role). */
export type AgentUnknownBucket = AgentActivityStats

export interface AgentsResponse {
  agents: AgentRoster[]
  unknown: AgentUnknownBucket
  note: string
}

/** `days`/`project`/`task` all default to unbounded/unfiltered — a roster
 * view is a lifetime/overview surface, not a rolling window, exactly like
 * the backend's own `agents_summary(days=None)` default. */
export function fetchAgents(days?: number, project?: string, task?: string): Promise<AgentsResponse> {
  const params = new URLSearchParams()
  if (days != null) params.set('days', String(days))
  if (project) params.set('project', project)
  if (task) params.set('task', task)
  const query = params.toString()
  return apiFetch<AgentsResponse>(`/v1/agents${query ? `?${query}` : ''}`, { on403: 'forbidden' })
}

/** One row of the `lessons` table (`state_service.list_lessons_by_tenant`).
 * `text`/`category` are LLM-distilled free text — UNTRUSTED, same trust
 * class as `PanelData`/`GraphDetail` elsewhere in this app; render via plain
 * JSX interpolation only. `validated` is the raw SQLite `INTEGER` (0/1), not
 * a JSON boolean — treat any non-zero value as `true`. */
export interface Lesson {
  id: number
  run_id: number | null
  project: string | null
  role: string | null
  task: string | null
  source_verdict_id: number | null
  source_interaction_id: number | null
  text: string | null
  score: number | null
  confidence: number | null
  category: string | null
  validated: number
  use_count: number
  created_at: string | null
}

export interface LessonRoleAggregation {
  total: number
  validated: number
  use_count: number
  /** `null` when the role has zero lessons with a non-null `score` in the
   * window — never a fabricated average. */
  avg_score: number | null
}

export interface LessonsResponse {
  lessons: Lesson[]
  by_role: Record<string, LessonRoleAggregation>
}

export function fetchLessons(role?: string, limit = 50): Promise<LessonsResponse> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (role) params.set('role', role)
  return apiFetch<LessonsResponse>(`/v1/lessons?${params.toString()}`, { on403: 'forbidden' })
}

/** One row of the `verdicts` table (`state_service.list_verdicts`).
 * `summary` is caller-supplied free text (e.g. reviewer summaries) —
 * UNTRUSTED, same trust class as `Lesson.text` above; render via plain JSX
 * interpolation only. `decision` is `null` when no confident decision was
 * reached (a fail-closed "blocking" outcome, see `orchestrator.py`'s
 * `Verdict` docstring) — a non-`"ACCEPT"` (case-insensitive) or `null`
 * decision is the "non-nominal" signal `AgentsView`'s severity stripe uses. */
export interface Verdict {
  id: number
  run_id: number | null
  project: string | null
  task: string | null
  role: string | null
  kind: string | null
  decision: string | null
  confidence: number | null
  summary: string | null
  timestamp: string | null
}

export interface VerdictRoleAggregation {
  total: number
  decision_counts: Record<string, number>
  kind_counts: Record<string, number>
}

export interface VerdictsResponse {
  verdicts: Verdict[]
  by_role: Record<string, VerdictRoleAggregation>
}

export function fetchVerdicts(role?: string, limit = 50): Promise<VerdictsResponse> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (role) params.set('role', role)
  return apiFetch<VerdictsResponse>(`/v1/verdicts?${params.toString()}`, { on403: 'forbidden' })
}

