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
  /** Steps that could not have cost anything (shell runner, skipped stage) —
   *  counted separately so the "total understated" warning stays truthful. */
  unpriceable_steps?: number
  /** Why steps are unpriced, by cause: `no_usage_captured` (the model is in
   *  the price map but no tokens were recorded), `no_price_for_model`, or
   *  `no_model_recorded`. The banner must name the right subsystem. */
  unpriced_reasons?: Record<string, number>
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
  /** Which BASIS the figures came from. Everything else here is the envelope
   * (`steps.cost_usd`, self-reported by the agent). OTel exports the same
   * spend independently, per API request — the two are NEVER added, which
   * would double-count. Optional: a server older than this field omits it. */
  basis?: CostBasisReport | null
}

export function fetchAnalyticsCost(days = 30): Promise<AnalyticsCost> {
  return apiFetch<AnalyticsCost>(`/v1/analytics/cost?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/analytics/whales — HP-81. Top-N individual model steps by spend,
// then prompt tokens. Aggregates on `/v1/analytics/cost` hide a $1.50 /
// 300k-token call inside "claude · 30d". Envelopes only — never prompt
// bodies. Shape transcribed from `analytics_service.cost_whales`.
// ---------------------------------------------------------------------------

export interface WhaleStep {
  step_id: number
  run_id: number
  project: string
  task: string
  step: string
  provider: string
  model: string
  timestamp: string | null
  input_tokens: number
  output_tokens: number
  cost_usd: number
  /** False when `_step_cost` could not price the step — shown so a huge
   * unpriced call never reads as a cheap one. */
  priced: boolean
}

export interface AnalyticsWhales {
  whales: WhaleStep[]
  limit: number
}

export function fetchAnalyticsWhales(days = 30, limit = 20): Promise<AnalyticsWhales> {
  return apiFetch<AnalyticsWhales>(`/v1/analytics/whales?days=${days}&limit=${limit}`)
}

// ---------------------------------------------------------------------------
// GET /v1/sessions/cost — per-run cost split by what was actually billed.
//
// A total answers "how much" and cannot answer "where did it go". Measured on
// one review dispatch: 516 982 cache-read tokens against 3 040 input and
// 20 455 output. As volume the reviewers look like they read too much; as
// cost they write a lot and the reading is cached and cheap. Only the split
// tells them apart — and the volume reading already sent one optimisation
// effort at the wrong parameter.
// ---------------------------------------------------------------------------

export interface SessionCostComponents {
  input: number
  output: number
  cache_read: number
  cache_write: number
}

export interface SessionCost {
  run_id: number
  project: string
  task: string
  started_at: string
  status: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost_usd: number
  /** Steps that plausibly cost something and could not be priced. Shown so a
   * partly-unpriceable session never reads as a cheap one. */
  unpriced_steps: number
  by_component: SessionCostComponents
}

export interface SessionCostsResponse {
  sessions: SessionCost[]
  total_sessions: number
}

export function fetchSessionCosts(days = 30, limit = 25): Promise<SessionCostsResponse> {
  return apiFetch<SessionCostsResponse>(`/v1/sessions/cost?days=${days}&limit=${limit}`)
}

// ---------------------------------------------------------------------------
// GET /v1/providers/fallbacks — recent HP-70 provider fallbacks (HP-73).
// The queryable companion to HP-70's otherwise-invisible fallback: which
// provider fell over, how often, when last, and why. Aggregated by source
// provider from durable `provider.fallback` events (HP-40 bus).
// ---------------------------------------------------------------------------

export interface ProviderFallback {
  provider: string
  count: number
  last_at: string | null
  last_reason: string | null
  last_to: string | null
}

export function fetchProviderFallbacks(
  hours = 24,
): Promise<{ hours: number; providers: ProviderFallback[] }> {
  return apiFetch<{ hours: number; providers: ProviderFallback[] }>(
    `/v1/providers/fallbacks?hours=${hours}`,
  )
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
  /** Steps that could not have cost anything (shell runner, skipped stage) —
   *  counted separately so the "total understated" warning stays truthful. */
  unpriceable_steps?: number
  /** Why steps are unpriced, by cause: `no_usage_captured` (the model is in
   *  the price map but no tokens were recorded), `no_price_for_model`, or
   *  `no_model_recorded`. The banner must name the right subsystem. */
  unpriced_reasons?: Record<string, number>
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
  /** Attempts headroom made and declined. Zero compressions WITH skips means
   *  it ran and found nothing worth rewriting; zero of both means it never ran. */
  total_skipped?: number
  total_attempts?: number
  skip_reasons?: Record<string, number>
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

/** The compression proxy's own `/stats`, verbatim. Shape is the proxy's, not
 * ours, so only the fields the view actually reads are typed — the rest ride
 * along untouched rather than being re-declared and drifting. */
export interface ProxyEfficiency {
  summary?: {
    api_requests?: number
    mode?: string
    compression?: {
      requests_compressed?: number
      total_tokens_removed?: number
      avg_compression_pct?: number
    }
    cost?: {
      without_headroom_usd?: number
      with_headroom_usd?: number
      total_saved_usd?: number
      savings_pct?: number
    }
  }
}

/** One step that creates more prompt cache than it ever reads back.
 *
 * `amortisation` is the MEDIAN per run, not the total. Summing hid the real
 * case entirely: `ceo intake` is pathological on nine runs out of ten, and a
 * single outlier that read 326 696 tokens lifted the sum over the floor. */
export interface UnamortisedStep {
  step: string
  runs: number
  cache_read: number
  cache_creation: number
  amortisation: number
  /** Median turns. Optional: a server older than this field reports the
   * ratio without the evidence for it. */
  turns?: number
}

export interface CacheEfficiency {
  steps: number
  /** `null`, never 0, when nothing has been measured — a rate of zero reads
   * as "the cache never works", which is a far louder claim than "no model
   * step has run yet". */
  hit_rate: number | null
  cache_read: number
  cache_creation: number
  /** Below the amortisation floor with turns to spare, so the prefix could
   * have been read back and was not. Actionable: reorder the prompt. */
  unamortised: UnamortisedStep[]
  /** Below the floor over too few turns for the ratio to mean anything — a
   * short step sits there by construction. Reported so the panel can say
   * "we looked, there is nothing to do", which is not the same as omitting
   * the step. Optional: absent from a server older than the split. */
  single_pass?: UnamortisedStep[]
  /** Below the floor with an unknown turn count — every row written before
   * the column existed. Counted, never resolved by guess. Without this the
   * table simply looks empty, which reads as "solved". */
  unclassified?: number
}

export interface EfficiencySummary {
  headroom: HeadroomEfficiency
  rtk: RtkEfficiency | null
  /** `null` means the proxy is unconfigured, unreachable, or answered
   * garbage — never "it compressed nothing". It sits on the critical path of
   * every agent call and can fall back to a direct one silently, so this is
   * the only surface that would show it had gone away. */
  proxy: ProxyEfficiency | null
  /** Prompt cache — the only source here measured from our OWN telemetry
   * rather than a tool's self-report, and the one that found a step paying
   * full price ten times behind a healthy-looking 85% aggregate. Optional:
   * a server older than this field omits it. */
  cache?: CacheEfficiency | null
  /** Context truncation, which existed only as a `logger.warning` until now.
   * Run 639: `cap` mode kept the TAIL of the joined prior context, ~90% of the
   * run vanished with both verdicts the release gate needed, and the gate then
   * refused a release on a clearance that HAD been given — a week to diagnose,
   * because nothing surfaced it. Optional: a server older than this field
   * omits it. */
  truncation?: TruncationEfficiency | null
}

/** Recorded context truncations. `recorded: 0` means nothing was WRITTEN
 * DOWN — never that nothing was truncated, and the view must say so in those
 * words. `null` on every field means the query itself failed, which is a third
 * state again. */
/** Two probes for the things that fail by GOING QUIET. `/plugins/health`
 * reports what loaded; these report whether two systems that produce
 * continuously are still producing. */
export interface HealthProbes {
  agent_surface: AgentSurfaceProbe
  otel: OtelProbe
}

export interface AgentSurfaceProbe {
  /** `not_configured` is the DEFAULT and is not a fault — a red badge on every
   * deployment that never asked for a live agent surface teaches people to
   * ignore the badge. `unreachable` is the state worth acting on. */
  state: 'not_configured' | 'ok' | 'unreachable' | 'unknown_backend' | 'unknown'
  backend: string | null
}

export interface OtelProbe {
  /** `never_arrived` points at configuration; `stale` points at an exporter
   * that used to work and stopped. Different investigations, so different
   * answers. */
  state: 'never_arrived' | 'ok' | 'stale' | 'unknown'
  /** Healthy forever once an exporter has ever worked — which is exactly why
   * it is not the figure to trust. */
  rows: number | null
  age_hours: number | null
}

export async function fetchHealthProbes(): Promise<HealthProbes> {
  return apiFetch<HealthProbes>('/v1/health/probes', { on403: 'forbidden' })
}

/** `GET /v1/health` — the service's own vitals, distinct from plugin health:
 * `database` ("ok" or "error: …"), `runners` ("ok (N defined)"), and one
 * `dep:<name>` entry per OPTIONAL dependency ("available" / "not installed").
 * A missing optional dependency is a choice, not a fault — the view must
 * render it neutrally (the #582/#583 polarity lesson). */
export interface ServiceHealth {
  status: 'ok' | 'degraded'
  checks: Record<string, string>
}

export function fetchServiceHealth(): Promise<ServiceHealth> {
  return apiFetch<ServiceHealth>('/v1/health')
}

/** Two readings of the SAME spend, kept apart. There is deliberately no
 * combined total: adding them double-counts. */
export interface CostBasisReport {
  /** `null` means NOT MEASURED — no step ever reported. Zero dollars is a
   * different statement: a period in which nothing was spent. */
  envelope: CostBasisFigure | null
  otel: CostBasisFigure | null
  /** False whenever the two windows differ or either is unknown. On the box
   * today the envelope starts 2026-07-26 and OTel only 2026-08-10, which puts
   * a 2.4x gap between two totals that are both correct. */
  comparable: boolean
  /** `null` unless the windows match — a ratio across different periods is a
   * number that means nothing and invites the wrong conclusion. Zero is a
   * finding: the two paths agree. */
  divergence_pct: number | null
}

export interface CostBasisFigure {
  total_usd: number
  count: number
  first: string | null
  last: string | null
}

export interface TruncationEfficiency {
  recorded: number | null
  dropped_chars: number | null
  /** The WORST single stage, never an average: the point of this figure is to
   * name the one stage whose output is blowing the budget, and an average is
   * exactly the statistic that hides it. */
  worst_stage_chars: number | null
  /** Whose output to go and read. A row with no role never wins it. */
  worst_role: string | null
  /** `derived` from the model's real window vs `fallback` to a configured
   * constant vs `unknown` for rows written before the basis was recorded — a
   * gap, not a guess. */
  by_basis: Record<string, number>
}

export function fetchEfficiency(days = 30): Promise<EfficiencySummary> {
  return apiFetch<EfficiencySummary>(`/v1/efficiency?days=${days}`)
}

// ---------------------------------------------------------------------------
// GET /v1/plugins/health
// ---------------------------------------------------------------------------

export type PluginHealthStatus = 'ok' | 'degraded' | 'error'

/** Evidence a plugin actually ran, from `hivepilot/services/plugin_activity.py`.
 *
 * `last_used` is a naive UTC timestamp (the SQLite `CURRENT_TIMESTAMP`
 * convention) — always render it through `@/lib/format-time`, which stamps
 * naive strings as UTC. `new Date()` would read them as local time and report
 * a plugin used minutes ago as hours old. */
export interface PluginActivity {
  last_used: string | null
  /** Events inside `window_days`. `last_used` is NOT window-bounded, so a
   * long-idle plugin reads as a real date here with `events: 0`. */
  events: number
  window_days: number
  /** The tables the numbers came from, shown to the operator rather than
   * asking them to trust a badge. */
  evidence: string
}

export interface PluginHealthEntry {
  name: string
  /** Whether the plugin is installed and configured — NOT whether it works.
   * `headroom` and `mem0` both sat at `ok` for weeks while failing every
   * call, which is why `activity` exists as a separate answer. */
  status: PluginHealthStatus
  detail: string
  /** False when the plugin records no telemetry (`rtk`, `gh` are PATH
   * checks). The UI must then say the check is presence-only rather than let
   * a green badge imply more. */
  activity_available: boolean
  /** `null` has two causes, split by `activity_available`: not measurable at
   * all (false), or measurable but the read failed (true). Neither is the
   * same as `events: 0`, which is a real reading meaning "measured, and it
   * has done nothing". */
  activity: PluginActivity | null
}

/** A plugin that is enabled AND installed and did not load.
 *
 * The third state. `check_all()` only covers REGISTERED plugins, and a
 * capability-denied plugin is rolled back before registration — so it appears
 * in neither `plugins` nor `disabled`. An operator could enable it, watch the
 * toggle succeed, and find it nowhere.
 *
 * Seen live: `token_savior` loads under the services' capability policy and is
 * denied under a CLI environment that lacks it. Same plugin, same flag,
 * opposite outcome, and the UI showed the same thing either way — nothing. */
export interface PluginDenied {
  name: string
  source: string | null
  error: string
  /** A denial an operator cannot act on is only marginally better than
   * silence. */
  remediation: string
}

export interface PluginsHealthResponse {
  plugins: PluginHealthEntry[]
  disabled: string[]
  /** Optional because an API that has not been redeployed yet does not send
   * it. Pollen's bundle ships with the engine but a host can lag, and a hard
   * requirement here would blank the whole Health tab against an older
   * backend — trading a missing section for a missing page. */
  denied?: PluginDenied[]
  /** Curated plugins written in the repo but not fetched onto this host.
   * Plugins are not shipped in the wheel, so a merge does not install them —
   * listing only what IS installed answers "what is on" while hiding "what
   * exists", which is how ~23 written plugins sat inert here unnoticed.
   * Optional for the same reason as `denied`. */
  not_installed?: string[]
}

/** One curated plugin, as a card needs it.
 *
 * `/plugins/health` reports what LOADED; this reports what EXISTS. ~23 of the
 * curated plugins are written and not installed on a given host, and that is
 * exactly the set an operator wants to browse and turn on. */
export interface PluginCatalogEntry {
  name: string
  description: string
  /** `pip` / `binary` / `config` — what KIND of thing must be present. */
  prereq_kind: string
  /** The exact thing to install, in the operator's own terms. HivePilot never
   * installs it: a `pip install` triggered from a web switch runs arbitrary
   * package code as the service user, and a heavy one has wedged this
   * project's production host before. */
  prereq_detail: string
  installed: boolean
  enabled: boolean
  env_flag: string
}

export interface PluginCatalogResponse {
  plugins: PluginCatalogEntry[]
}

export function fetchPluginCatalog(): Promise<PluginCatalogResponse> {
  return apiFetch<PluginCatalogResponse>('/v1/plugins/catalog')
}

export interface PluginInstallResult {
  name: string
  installed_to: string
  enabled: boolean
  /** Always true. `PluginManager` scans once at construction, so a freshly
   * installed plugin is inert until the process restarts — a UI implying
   * otherwise sends the operator hunting a plugin that is on disk, enabled,
   * and doing nothing. */
  restart_required: boolean
  prereq_detail: string
}

/** Fetch a curated plugin file onto the host and persist its enable flag.
 * Admin-only, and restricted server-side to the curated registry. */
// ---------------------------------------------------------------------------
// Agent binaries (box-only admin surface). The POST carries {"consent": true}
// — the button's signature on the decision, the non-interactive replacement
// for agent_install.py's TTY "yes". Only curated registry kinds ever execute;
// the server validates before anything runs.
// ---------------------------------------------------------------------------

export interface AgentAdminEntry {
  kind: string
  name: string
  vendor: string
  binary: string
  docs_url: string
  installable: boolean
  updatable: boolean
  /** THIS process's shutil.which — the service's own view, the one that
   * decides whether a runner registers. Installed-but-false is the grok trap. */
  on_service_path: boolean
  installed_version: string | null
  /** Tri-state on purpose: present/absent where a store was VERIFIED,
   * "unknown" everywhere else — never guessed into a boolean. */
  auth: 'present' | 'absent' | 'unknown'
  login_available: boolean
}

export interface AgentsAdminResponse {
  agents: AgentAdminEntry[]
}

export interface AgentActionResult {
  kind: string
  action: string
  ok: boolean
  exit_code?: number
  version_before: string | null
  version_after: string | null
  on_service_path: boolean
  detail?: string
}

export function fetchAgentsAdmin(): Promise<AgentsAdminResponse> {
  return apiFetch<AgentsAdminResponse>('/v1/agents/admin')
}

export interface AgentLoginResult {
  kind: string
  /** The validation URL to open — the ONLY thing returned from the flow's
   * output. null = the flow printed nothing URL-shaped in the window. */
  url: string | null
  log: string
}

export function agentLogin(kind: string): Promise<AgentLoginResult> {
  return apiFetch<AgentLoginResult>(`/v1/agents/${encodeURIComponent(kind)}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ consent: true }),
  })
}

export function agentAction(
  kind: string,
  action: 'install' | 'update',
): Promise<AgentActionResult> {
  return apiFetch<AgentActionResult>(
    `/v1/agents/${encodeURIComponent(kind)}/${action}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ consent: true }),
    },
  )
}

export function installPlugin(name: string): Promise<PluginInstallResult> {
  return apiFetch<PluginInstallResult>(`/v1/plugins/${encodeURIComponent(name)}/install`, {
    method: 'POST',
  })
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
  /** Latest step timestamp — the run's "heartbeat" (when it last did anything,
   * distinct from when it started). Null for a run with no steps yet. */
  last_activity_at?: string | null
  /** How many steps the run has recorded so far — a cheap progress signal. */
  step_count?: number
  /** Untrusted free text (redacted server-side, but still opaque to the
   * UI's trust model) — never render this. */
  detail?: string | null
}

/** Recent runs. `limit` is caller-chosen — the board was pinned to 50 with no
 * way to ask for fewer, so watching a single pipeline meant reading 50 cards.
 * The API bounds it to 1-500 and rejects anything else outright. */
export function fetchRuns(limit?: number): Promise<RunSummary[]> {
  const query = limit ? `?limit=${encodeURIComponent(String(limit))}` : ''
  return apiFetch<RunSummary[]>(`/v1/runs${query}`, { on403: 'forbidden' })
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
  /** Steps that could not have cost anything (shell runner, skipped stage) —
   *  counted separately so the "total understated" warning stays truthful. */
  unpriceable_steps?: number
  /** Why steps are unpriced, by cause: `no_usage_captured` (the model is in
   *  the price map but no tokens were recorded), `no_price_for_model`, or
   *  `no_model_recorded`. The banner must name the right subsystem. */
  unpriced_reasons?: Record<string, number>
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
/** One cause a step can carry no role, with the spend it accounts for. */
export interface UnknownBreakdownPart {
  step_count: number
  cost_usd: number
}

/** Why the roleless steps are roleless.
 *
 * Only `attribution_gap` is a defect. `no_model` (shell steps — no agent was
 * involved) and `skipped` (never ran) are structural and correctly excluded
 * from every per-role figure. The bucket used to be one undifferentiated
 * number labelled as legacy pre-attribution history; on real data that was
 * wrong for every row in it, and the handful of genuinely unattributed model
 * invocations — the ones carrying real cost — were invisible inside it.
 *
 * Counts sum to the bucket's own `step_count`. */
export interface UnknownBreakdown {
  no_model: UnknownBreakdownPart
  skipped: UnknownBreakdownPart
  attribution_gap: UnknownBreakdownPart
}

export interface AgentUnknownBucket extends AgentActivityStats {
  breakdown: UnknownBreakdown
}

export interface AgentsResponse {
  agents: AgentRoster[]
  unknown: AgentUnknownBucket
  note: string
}

/** `days`/`project`/`task` all default to unbounded/unfiltered — a roster
 * view is a lifetime/overview surface, not a rolling window, exactly like
 * the backend's own `agents_summary(days=None)` default. */
/** Per-role LIVE state from the herdr/Orca surface (`GET /v1/agents/live`).
 *
 * `configured: false` carries a `detail` saying WHY — no backend set, or a
 * name we do not know. Render that reason: an agent shown as `unknown`
 * without one reads as a bug in the dashboard rather than a deployment that
 * has no agent surface configured.
 *
 * `state` is one of herdr's five (`idle`/`working`/`blocked`/`done`/
 * `unknown`); anything the backend returns that we do not model arrives as
 * `unknown` rather than reaching the UI as a state of its own. */
export interface AgentLiveResponse {
  configured: boolean
  detail: string
  agents: { role: string; state: string }[]
}

export function fetchAgentsLive(): Promise<AgentLiveResponse> {
  return apiFetch<AgentLiveResponse>('/v1/agents/live', { on403: 'forbidden' })
}

/** Send text to a live agent (`POST /v1/agents/{role}/message`).
 *
 * Resolves with `dispatched`, never `delivered`: the send is fire-and-forget
 * at the API layer, and the UI must not claim the agent received it. */
export function sendAgentMessage(
  role: string,
  text: string,
): Promise<{ dispatched: boolean; detail?: string }> {
  return apiFetch<{ dispatched: boolean; detail?: string }>(
    `/v1/agents/${encodeURIComponent(role)}/message`,
    { method: 'POST', body: JSON.stringify({ text }), on403: 'forbidden' },
  )
}

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

// ---------------------------------------------------------------------------
// GET /v1/partitions, GET /v1/partitions/{id},
// POST /v1/partitions/{id}/preview, POST /v1/partitions/{id}/ratify —
// propose -> ratify -> dispatch PRD, Sprint 4 (the Pollen ratification view).
//
// Shapes transcribed from `hivepilot/services/api_service.py`'s
// `PartitionSummary`/`PartitionDetail`/`PartitionPreviewResponse`/
// `PartitionRatifyResponse`. Read the large module comment above
// `PartitionSummary` there — the RBAC floors and the tenant-isolation rules
// — before changing anything here.
//
// The one contract that matters on this side: **none of the ratification
// rules live in the browser.** `previewPartition` is a dry run of the SAME
// `validate_ratification` the real gate runs; `PartitionPreview.ok` is that
// gate's own answer, and it is what the dispatch control gates on. The UI
// re-derives no rule — not the outward footprint (`outward_actions` is
// resolved from LIVE pipeline config, never from a task's self-declared
// `outward` flag), not the cost ceiling, not the effective parallelism.
//
// RBAC: the two GETs need `run`, `preview`/`ratify` need `approve`. All of
// them opt into `on403: 'forbidden'` so a role-specific refusal renders a
// graceful message instead of clearing an otherwise-valid token out from
// under every other tab (same posture as `fetchApprovals`).
// ---------------------------------------------------------------------------

export interface PartitionSummary {
  id: string
  tenant: string
  /** `proposed | ratified | dispatching | completed | failed | vetoed | expired` */
  status: string
  source_kind: string | null
  source_ref: string | null
  proposed_digest: string | null
  ratified_digest: string | null
  outward_consent: boolean
  ratified_by: string | null
  ratified_at: string | null
  created_ts: string | null
  updated_ts: string | null
}

/** One journal row. `pr_url` is `null` when the forge did not report a URL —
 * rendered as an em-dash, NEVER a fabricated link (see the `open_pr -> str |
 * null` widening in `hivepilot/forges/provider.py`). */
export interface PartitionTaskRow {
  task_id: string
  status: string
  run_id: number | null
  queue_id: number | null
  attempt: number
  claimed_by: string | null
  claimed_at: string | null
  pr_url: string | null
  cost_usd: number | null
  wall_clock_seconds: number | null
}

/**
 * What "N parallel agents" actually means on THIS host.
 *
 * `runner_throttle` caps the `claude` runner kind at
 * `settings.claude_max_concurrency`, whose default is **1** — so a plan
 * asking for `max_parallel: 3` is one agent three times on a default
 * install. `effective` is the computed truth and is what the view must
 * show; showing `requested` alone would be a lie. `notes` explains WHY, in
 * the backend's own words — rendered verbatim, never re-worded.
 */
export interface PartitionParallelism {
  requested: number
  effective: number
  concurrency_limit: number
  runner_cap: number
  runner_kinds: string[]
  notes: string[]
}

export interface PartitionDetail extends PartitionSummary {
  proposed_json: string | null
  ratified_json: string | null
  ratified_diff: string | null
  outward_actions: string[]
  total_cost_usd: number | null
  waves: string[][]
  /** `null` when the stored plan could not be parsed — the view renders an
   * em-dash, never a plausible-looking `1`. */
  parallelism: PartitionParallelism | null
  tasks: PartitionTaskRow[]
}

/**
 * The gate's verdict for a plan the operator has NOT submitted yet.
 *
 * `ok` is `validate_ratification`'s own answer. A refusal arrives as HTTP
 * 200 with `ok: false` deliberately (a 4xx would be indistinguishable from a
 * network failure here, and would discard the footprint/waves/parallelism
 * the operator needs precisely while being refused) — the gate's real
 * `status_code` still travels, as data.
 */
export interface PartitionPreview {
  ok: boolean
  /** `malformed | referential | policy_denied | consent_required |
   * digest_mismatch | not_found`, or `null` when `ok`. */
  code: string | null
  status_code: number | null
  /** The refusal message, verbatim from the gate. UNTRUSTED only in the
   * sense that it may quote operator-supplied plan content — rendered via
   * plain JSX interpolation, never `dangerouslySetInnerHTML`. */
  detail: string | null
  outward_actions: string[]
  total_cost_usd: number | null
  waves: string[][]
  task_ids: string[]
  parallelism: PartitionParallelism | null
}

export interface PartitionRatifyResult {
  partition_id: string
  status: string
  ratified_digest: string
  outward_actions: string[]
  outward_consent: boolean
  task_ids: string[]
  diff: string
  warnings: string[]
  idempotent: boolean
  dispatching: boolean
  parallelism: PartitionParallelism | null
}

export function fetchPartitions(statusFilter?: string, limit = 50): Promise<PartitionSummary[]> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (statusFilter) params.set('status_filter', statusFilter)
  return apiFetch<PartitionSummary[]>(`/v1/partitions?${params.toString()}`, {
    on403: 'forbidden',
  })
}

export function fetchPartition(id: string): Promise<PartitionDetail> {
  return apiFetch<PartitionDetail>(`/v1/partitions/${encodeURIComponent(id)}`, {
    on403: 'forbidden',
  })
}

/** Dry-run the gate over the plan currently in the editor. Changes nothing.
 * `outwardConsent` mirrors the checkbox's live state so the verdict shown is
 * the verdict the operator would actually get. */
export function previewPartition(
  id: string,
  partitionJson: string,
  outwardConsent: boolean,
): Promise<PartitionPreview> {
  return postJson<PartitionPreview>(`/v1/partitions/${encodeURIComponent(id)}/preview`, {
    partition_json: partitionJson,
    outward_consent: outwardConsent,
  })
}

/** Ratify AND dispatch (the backend's `dispatch` defaults to `true` — spec
 * §12.5: a ratified-but-undispatched partition is a dangling state). This is
 * the authoritative, fail-closed gate; the preview above never substitutes
 * for it. */
export function ratifyPartition(
  id: string,
  body: {
    partition_json: string
    outward_consent: boolean
    expected_digest: string | null
  },
): Promise<PartitionRatifyResult> {
  return postJson<PartitionRatifyResult>(`/v1/partitions/${encodeURIComponent(id)}/ratify`, body)
}

/**
 * Prompt-cache economics, from the agent CLI's own OTLP metrics.
 *
 * Deliberately a median and a count below break-even, never a fleet ratio: an
 * aggregate is dominated by whichever session read the most, which is how an
 * 85% hit rate coexisted with 1.7M tokens of creation never read back.
 */
export interface CacheReport {
  sessions: number
  median_amortisation: number
  below_one: number
  wasted_tokens: number
  healthy: boolean
  worst: {
    session_id: string
    model: string | null
    created: number
    read: number
    amortisation: number
  } | null
}

export function fetchCacheReport(days = 30): Promise<CacheReport> {
  return apiFetch<CacheReport>(`/v1/telemetry/cache?days=${days}`)
}

/** Recall/store counters for one memory backend. */
export interface MemoryBackendStats {
  searches: number
  /** Recalls that came back with nothing — the KPI that survives comparison.
   * A full top-k means the CAP was hit, not that k relevant things exist. */
  empty_searches: number
  stores: number
  reads: number
  last_activity: string | null
  actors: number
}

export interface MemoryBackendsResponse {
  days: number
  backends: Record<string, MemoryBackendStats>
  /** Whether a backend sends work off the host. A property of the backend,
   * and the one fact an operator most needs beside these counters. */
  egress: Record<string, boolean>
}

export function fetchMemoryBackends(days = 30): Promise<MemoryBackendsResponse> {
  return apiFetch<MemoryBackendsResponse>(`/v1/memory/backends?days=${days}`)
}

/** One agent's turn in a run's conversation. */
export interface ConversationMessage {
  interaction_id: number
  actor: string
  role: string | null
  action: string
  body: string
  at: string | null
}

export interface ConversationThread {
  run_id: number
  roles: string[]
  messages: ConversationMessage[]
}

export interface ConversationRun {
  run_id: number
  project: string | null
  started_at: string | null
  message_count: number
  roles: string[]
}

export interface ConversationRunsResponse {
  runs: ConversationRun[]
}

export function fetchConversationRuns(limit = 25): Promise<ConversationRunsResponse> {
  return apiFetch<ConversationRunsResponse>(`/v1/conversations?limit=${limit}`)
}

export function fetchConversationThread(runId: number): Promise<ConversationThread> {
  return apiFetch<ConversationThread>(`/v1/conversations/${runId}`)
}

/** Record an operator instruction for a role, feeding its NEXT run.
 *
 * Not a message to a running agent — by the time a thread is readable its
 * agents have exited. This appends to the role's corrections file, attributed
 * to the operator. */
export function replyToRole(role: string, text: string): Promise<{ role: string; written_to: string }> {
  return apiFetch<{ role: string; written_to: string }>('/v1/conversations/reply', {
    method: 'POST',
    body: JSON.stringify({ role, text }),
  })
}

// ---------------------------------------------------------------------------
// Espaces — conversation rooms (HP-45). GET /v1/spaces[/{id}[/messages]],
// POST /v1/spaces, POST /v1/spaces/{id}/messages. Mirrors the shapes in
// `hivepilot/services/state_service.py` + `api_service.py`.
// ---------------------------------------------------------------------------

export interface SpaceParticipant {
  type: string
  id?: string | null
}

export interface SpaceSummary {
  id: number
  kind: string
  title?: string | null
  participants: SpaceParticipant[]
  message_count?: number
  last_message_at?: string | null
  created_at?: string
  updated_at?: string
  tenant?: string
}

export interface SpaceAction {
  label: string
  detail?: string | null
}

export interface SpaceMessage {
  id: number
  space_id: number
  sender_type: string
  sender_id?: string | null
  body: string
  /** Optional collapsible tool-action trace (HP-47). */
  actions?: SpaceAction[] | null
  created_at: string
}

export function fetchSpaces(): Promise<SpaceSummary[]> {
  return apiFetch<{ spaces: SpaceSummary[] }>('/v1/spaces').then((r) => r.spaces)
}

export function fetchSpaceMessages(spaceId: number, after = 0): Promise<SpaceMessage[]> {
  return apiFetch<{ messages: SpaceMessage[] }>(
    `/v1/spaces/${spaceId}/messages?after=${after}`,
  ).then((r) => r.messages)
}

/** Post a human message to a space. `run`-gated server-side — a `read`-only
 * token gets `ApiForbiddenError` (not logged out) via `on403: 'forbidden'`. */
export function postSpaceMessage(spaceId: number, body: string): Promise<{ id: number }> {
  return apiFetch<{ id: number }>(`/v1/spaces/${spaceId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ body }),
    on403: 'forbidden',
  })
}

export function createSpace(
  participants: SpaceParticipant[],
  opts: { kind?: string; title?: string } = {},
): Promise<SpaceSummary> {
  return apiFetch<SpaceSummary>('/v1/spaces', {
    method: 'POST',
    body: JSON.stringify({ participants, kind: opts.kind ?? 'dm', title: opts.title }),
    on403: 'forbidden',
  })
}

// ---- Orchestrator: decomposition + mission strategies (HP-49 / HP-69) ------

/** A resolved execution/merge strategy preset — one mockup mode card. */
export interface MissionStrategyDetail {
  name: string
  stages: string[]
  dispatch: 'sequential' | 'parallel'
  merge: 'per_task' | 'per_branch' | 'final' | 'none'
  new_mission: boolean
  /** i18n key for the mockup's guarantee label. */
  guarantee: string
}

export interface MissionTask {
  id: string
  title: string
  role: string
  description?: string
  depends_on?: string[]
}

export interface MissionPlan {
  goal: string
  strategy: string
  strategy_detail: MissionStrategyDetail
  tasks: MissionTask[]
  roles_config?: Record<string, Record<string, unknown>>
}

export interface DecomposeResult {
  plan: MissionPlan
  space_id: number
}

export interface LaunchMissionResult extends DecomposeResult {
  runs: Record<string, number>
  mission_id: number
}

/** The catalog of strategy presets (mockup mode cards) + the default name. */
export function fetchMissionStrategies(): Promise<{
  strategies: MissionStrategyDetail[]
  default: string
}> {
  return apiFetch<{ strategies: MissionStrategyDetail[]; default: string }>(
    '/v1/orchestrator/strategies',
  )
}

/** Decompose a goal into a plan (PREVIEW — no spawn). `run`-gated. */
export function decomposeFeature(
  goal: string,
  project?: string,
  strategy?: string,
): Promise<DecomposeResult> {
  return postJson<DecomposeResult>('/v1/orchestrator/decompose', { goal, project, strategy })
}

/** Decompose + spawn each task as a background run. `run`-gated. */
export function launchMission(
  goal: string,
  project?: string,
  strategy?: string,
): Promise<LaunchMissionResult> {
  return postJson<LaunchMissionResult>('/v1/orchestrator/mission', { goal, project, strategy })
}

// ---------------------------------------------------------------------------
// MCP command center (HP-76). GET /v1/mcp/servers + /catalog, POST /import,
// POST /servers/{id}/probe, DELETE /servers/{id}. Shapes from mcp_registry
// + mcp_probe + state_service.mcp_servers.
// ---------------------------------------------------------------------------

export interface McpServer {
  id: number
  name: string
  transport: 'stdio' | 'http' | string
  command?: string | null
  args?: string[]
  url?: string | null
  env?: Record<string, string>
  source?: string
  last_probe_status?: string | null
  last_probe_detail?: string | null
  last_probe_at?: string | null
}

export interface McpCatalogEntry {
  name: string
  description: string
  transport: string
  command?: string | null
  args?: string[]
  url?: string | null
  paste: string
  installed: boolean
}

export function fetchMcpServers(): Promise<{ servers: McpServer[]; cost_note: string }> {
  return apiFetch<{ servers: McpServer[]; cost_note: string }>('/v1/mcp/servers')
}

export function fetchMcpCatalog(): Promise<{ catalog: McpCatalogEntry[] }> {
  return apiFetch<{ catalog: McpCatalogEntry[] }>('/v1/mcp/catalog')
}

export function importMcpConfig(text: string): Promise<{
  drafts: unknown[]
  servers: McpServer[]
  stripped_env_keys: string[]
}> {
  return postJson('/v1/mcp/import', { text })
}

export function addMcpFromCatalog(name: string): Promise<{ server: McpServer }> {
  return postJson('/v1/mcp/catalog/add', { name })
}

export function probeMcpServer(id: number): Promise<{ server: McpServer }> {
  return postJson(`/v1/mcp/servers/${id}/probe`, {})
}

export function deleteMcpServer(id: number): Promise<{ deleted: number }> {
  return apiFetch(`/v1/mcp/servers/${id}`, { method: 'DELETE', on403: 'forbidden' })
}

// ---------------------------------------------------------------------------
// HP-78 — onboarding: reuse what's already on the machine, verify first.
// ---------------------------------------------------------------------------

export interface LocalBackend {
  kind: string
  base_url: string
  reachable: boolean
  models: string[]
  error: string | null
}

export interface CliSession {
  kind: string
  state: string
  login_available: boolean
}

export interface OnboardingMachine {
  local: LocalBackend[]
  cli: CliSession[]
}

export interface ModelVerifyResult {
  ok: boolean
  target: string
  detail: string
  models: string[]
  error: string | null
}

export function fetchOnboardingMachine(): Promise<OnboardingMachine> {
  return apiFetch<OnboardingMachine>('/v1/onboarding/machine')
}

export function verifyModel(body: {
  provider?: string
  agent_kind?: string
  base_url?: string
}): Promise<ModelVerifyResult> {
  return postJson('/v1/models/verify', body)
}

export interface ModelConnectResult {
  ok: boolean
  provider: string
  env_key: string | null
  detail: string
  models: string[]
  saved: boolean
  error: string | null
}

export function connectModel(body: {
  provider: string
  api_key: string
  base_url?: string
}): Promise<ModelConnectResult> {
  return postJson('/v1/models/connect', { ...body, consent: true })
}
