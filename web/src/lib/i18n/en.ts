/**
 * English dictionary — the default language AND the fallback for any key
 * missing from another dictionary (`./fr.ts`). Flat, dot-namespaced keys
 * (`nav.overview`, `analytics.totalRuns`, ...) rather than nested objects —
 * simplest to keep `en`/`fr` in exact 1:1 key parity (see `fr.ts`, which is
 * typed `Record<TranslationKey, string>` against this file's key set).
 *
 * `{name}` placeholders are interpolated by `LanguageProvider`'s `t()`.
 */
export const en = {
  // ---- common ----------------------------------------------------------
  'common.load': 'Load',
  'common.enable': 'Enable',
  'common.disable': 'Disable',
  'common.working': 'Working…',
  'common.openNavigation': 'Open navigation',
  'common.expandSidebar': 'Expand sidebar',
  'common.collapseSidebar': 'Collapse sidebar',
  'common.switchToLightTheme': 'Switch to light theme',
  'common.switchToDarkTheme': 'Switch to dark theme',
  'common.switchToEnglish': 'Switch to English',
  'common.switchToFrench': 'Switch to French',
  'common.lastDays': 'Last {days} days',
  'common.lastDaysLower': 'last {days} days',
  'common.and': ' and ',

  // ---- header / shell ----------------------------------------------------
  'header.subtitle': 'HivePilot insight dashboard',

  // ---- nav -----------------------------------------------------------
  'nav.overview': 'Overview',
  'nav.agents': 'Agents',
  'nav.system': 'System',
  'nav.memory': 'Memory',
  'nav.panels': 'Panels',
  'nav.analytics': 'Analytics',
  'nav.cost': 'Cost',
  'nav.health': 'Health',
  'nav.mem0': 'Mem0',
  'nav.approvals': 'Approvals',
  'nav.runs': 'Runs',
  'nav.graph': 'Graph',

  // ---- health status words (shared: header pills + Health tab badges) --
  'health.status.ok': 'ok',
  'health.status.degraded': 'degraded',
  'health.status.error': 'error',

  // ---- Analytics view ----------------------------------------------------
  'analytics.volumeTitle': 'Volume & outcomes',
  'analytics.noRuns': 'No runs recorded in this window.',
  'analytics.totalRuns': 'Total runs',
  'analytics.succeeded': 'Succeeded',
  'analytics.runsCount': '{count} runs',
  'analytics.failed': 'Failed',
  'analytics.other': 'Other',
  'analytics.trendTitle': 'Trend',
  'analytics.trendDescription': 'Runs per day',
  'analytics.noTrend': 'No trend data for this window.',
  'analytics.durationTitle': 'Duration percentiles',
  'analytics.durationDescription': 'Finished runs, p50 / p95 / p99',
  'analytics.noDuration': 'No finished runs yet.',
  'analytics.hotspotsTitle': 'Step failure hotspots',
  'analytics.hotspotsDescription': 'Highest-failure-count steps first',
  'analytics.noHotspots': 'No step failures recorded.',
  'analytics.step': 'Step',
  'analytics.status': 'Status',
  'analytics.count': 'Count',
  'analytics.approvalLatencyTitle': 'Approval latency',
  'analytics.approvalLatencyDescription': 'Time from request to decision',
  'analytics.noApprovals': 'No actioned approvals yet.',
  'analytics.actionedApprovals': 'Actioned approvals',

  // ---- Cost view -----------------------------------------------------
  'cost.title': 'Cost & tokens',
  'cost.noCost': 'No cost data yet.',
  'cost.totalCost': 'Total cost',
  'cost.inputTokens': 'Input tokens',
  'cost.outputTokens': 'Output tokens',
  'cost.unpricedSteps': 'Unpriced steps',
  'cost.provider': 'Provider',
  'cost.steps': 'Steps',
  'cost.tokensInOut': 'Tokens (in/out)',
  'cost.costLabel': 'Cost',
  'cost.unpriced': 'Unpriced',
  'cost.model': 'Model',
  'cost.providerVolumeTitle': 'Provider & model volume',
  'cost.providerVolumeDescription': 'Step counts and outcome split',
  'cost.noProviderData': 'No provider/model data yet.',
  'cost.total': 'Total',
  'cost.succeeded': 'Succeeded',
  'cost.failed': 'Failed',

  // ---- Health view -----------------------------------------------------
  'health.title': 'Plugin health',
  'health.description': 'Process-global plugin status, same as `hivepilot plugins health`.',
  'health.restartNote': " Enable/disable applies on the server's next restart only.",
  'health.noPlugins': 'No plugins registered.',
  'health.disabledPlugins': 'Disabled plugins',
  'health.disablePending': 'disable pending · restart',
  'health.disabled': 'disabled',
  'health.insufficientRole': 'Insufficient role — your token can no longer toggle plugins.',
  'health.restartRequired': 'restart required',
  'health.restartTakesEffectTitle': 'Takes effect on next restart only — no live reload.',
  'health.pendingBadgeTitle':
    "Flagged to disable — takes effect on the server's next restart. Currently still active.",
  'health.restartAppliesTitle': "This change applies on the API server's next restart.",

  // ---- Graph view ------------------------------------------------------
  'graph.title': 'Graph',
  'graph.description':
    "Graph-native views of HivePilot's own state — pan/zoom the canvas, click a node for detail.",
  'graph.loadingSources': 'Loading sources…',
  'graph.noSources': 'No graph sources registered.',
  'graph.source': 'Source',
  'graph.loadingGraph': 'Loading graph…',
  'graph.requiresTokenLead': 'This source requires a',
  'graph.requiresTokenTail': 'token.',
  'graph.requiresTokenNote':
    'Your current token can still use the other Mirador tabs — only this graph source needs a higher role.',
  'graph.higherPrivilege': 'higher-privilege',
  'graph.aParameter': 'a parameter',
  'graph.parameters': 'parameters',
  'graph.missingParamHintLead': 'This source needs {countLabel} to load data. Enter',
  'graph.missingParamHintTail': 'above and click',
  'graph.failedToLoad': 'Failed to load this graph ({label}). Try again or choose a different source.',
  'graph.noNodes': 'This source has no nodes yet.',
  'graph.selectNodeForDetail': 'Select a node for detail.',
  'graph.loadingDetail': 'Loading detail…',
  'graph.nodeRequiresTokenLead': "This node's detail requires a",
  'graph.nodeRequiresTokenTail': 'token.',
} as const

/** Every valid translation key — derived from `en`, the single source of
 * truth for the dictionary's key set. `fr.ts` is typed against this. */
export type TranslationKey = keyof typeof en
