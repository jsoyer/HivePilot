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
  'common.loading': 'Loading…',
  'common.noDataYet': 'No data yet.',
  'common.project': 'Project',
  'common.task': 'Task',
  'common.status': 'Status',
  'common.run': 'Run',
  'common.actions': 'Actions',
  'common.processing': 'Processing…',
  'common.starting': 'Starting…',
  'common.stopping': 'Stopping…',
  'common.requiresRunRankLead': 'This view requires a',
  'common.requiresRunRankTail':
    '(or higher) token. Your current token can still use the other Mirador tabs — only this list needs a higher role.',

  // ---- header / shell ----------------------------------------------------
  'header.subtitle': 'HivePilot insight dashboard',
  'header.search': 'Search',

  // ---- command palette (P1b: Cmd+K / Ctrl+K) ----------------------------
  'palette.title': 'Command palette',
  'palette.placeholder': 'Search views and actions…',
  'palette.noResults': 'No matching commands.',
  'palette.actionsGroup': 'Actions',
  'palette.toggleTheme': 'Toggle theme (light/dark)',
  'palette.toggleLanguage': 'Switch language (EN/FR)',

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
  'analytics.noAttempts': '{count} skipped, no attempts',
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

  // ---- Mem0 view ---------------------------------------------------------
  'mem0.title': 'Mem0 memory search',
  'mem0.description': 'Semantic search over the mem0 store — requires an admin token',
  'mem0.searchPlaceholder': 'Search memories…',
  'mem0.searchAriaLabel': 'Search memories',
  'mem0.searchButton': 'Search',
  'mem0.searchHint': 'Enter a search query above to look up memories.',
  'mem0.requiresTokenLead': 'This view requires an',
  'mem0.requiresTokenTail': 'token.',
  'mem0.requiresTokenNote':
    'Your current token can still use the other Mirador tabs — only Mem0 search needs a higher role.',
  'mem0.notConfigured': 'mem0 is not configured.',
  'mem0.noResults': 'No memories found for that query.',
  'mem0.category': 'Category',
  'mem0.timestamp': 'Timestamp',
  'mem0.memory': 'Memory',

  // ---- Approvals view ------------------------------------------------------
  'approvals.descriptionCanApprove': 'Pending pipeline approvals — approve or deny below.',
  'approvals.descriptionReadOnly':
    'Pending pipeline approvals (read-only — an approve-rank token can act on these).',
  'approvals.noPending': 'No pending approvals.',
  'approvals.requested': 'Requested',
  'approvals.approve': 'Approve',
  'approvals.deny': 'Deny',
  'approvals.approveAriaLabel': 'Approve run {id}',
  'approvals.denyAriaLabel': 'Deny run {id}',
  'approvals.denialReasonAriaLabel': 'Denial reason for run {id}',
  'approvals.reasonPlaceholder': 'Reason for denial (required)…',
  'approvals.confirmDeny': 'Confirm deny',
  'approvals.insufficientRoleApprove':
    'Insufficient role — your token can no longer approve/deny this run.',

  // ---- Runs view -----------------------------------------------------------
  'runs.descriptionCanRun': 'Trigger a new run and watch its status update live.',
  'runs.descriptionReadOnly': 'Recent runs (read-only — a run-rank token can trigger new ones).',
  'runs.noRuns': 'No runs yet.',
  'runs.taskPlaceholder': 'e.g. deploy',
  'runs.projectPlaceholder': 'e.g. acme-web',
  'runs.extraPromptLabel': 'Extra prompt (optional)',
  'runs.extraPromptPlaceholder': 'Additional context for this run…',
  'runs.autoGitLabel': 'Auto-commit/push git actions',
  'runs.newRunButton': 'New Run',
  'runs.insufficientRoleCreate': 'Insufficient role — your token can no longer trigger runs.',
  'runs.stopButton': 'Stop',
  'runs.stopAriaLabel': 'Stop run {id}',
  'runs.stopConfirm': 'Stop run #{id} ({task} on {project})?',
  'runs.insufficientRoleStop': 'Insufficient role — your token can no longer stop this run.',
  'runs.started': 'Started',
  'runs.finished': 'Finished',
} as const

/** Every valid translation key — derived from `en`, the single source of
 * truth for the dictionary's key set. `fr.ts` is typed against this. */
export type TranslationKey = keyof typeof en
