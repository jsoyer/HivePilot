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
  // Accessible name for the mobile navigation drawer itself (the dialog),
  // as opposed to `common.openNavigation` which names the button that opens it.
  'common.navigation': 'Navigation',
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
  'common.cancel': 'Cancel',
  'common.processing': 'Processing…',
  'common.starting': 'Starting…',
  'common.stopping': 'Stopping…',
  'common.requiresRunRankLead': 'This view requires a',
  'common.requiresRunRankTail':
    '(or higher) token. Your current token can still use the other Pollen tabs — only this list needs a higher role.',

  // ---- header / shell ----------------------------------------------------
  // The eyebrow line under the "Pollen" wordmark — a short, plain
  // description of what the product is, not a slogan.
  'header.subtitle': 'HivePilot dashboard',
  'header.search': 'Search',

  // ---- command palette (P1b: Cmd+K / Ctrl+K) ----------------------------
  'palette.title': 'Command palette',
  'palette.placeholder': 'Search views and actions…',
  'palette.noResults': 'No matching commands.',
  'palette.actionsGroup': 'Actions',
  'palette.toggleTheme': 'Toggle theme (light/dark)',
  'palette.toggleLanguage': 'Switch language (EN/FR)',

  // ---- nav -----------------------------------------------------------
  'nav.atAGlance': 'At a glance',
  'nav.home': 'Home',
  'nav.overview': 'Overview',
  'nav.operate': 'Operate',
  'nav.system': 'System',
  'nav.memory': 'Memory',
  'nav.panels': 'Panels',
  'nav.analytics': 'Analytics',
  'nav.spend': 'Spend',
  'nav.cost': 'Cost',
  'nav.models': 'Models',
  'nav.efficiency': 'Efficiency',
  'nav.health': 'Health',
  'nav.approvals': 'Approvals',
  'nav.runs': 'Runs',
  'nav.autopilot': 'Autopilot',
  'nav.partitions': 'Partitions',
  'nav.agents': 'Agents',
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
  'cost.steps': 'Steps',
  'cost.tokensInOut': 'Tokens (in/out)',
  'cost.costLabel': 'Cost',
  'cost.model': 'Model',
  // Accessible names for the horizontal scroll region a wide table becomes on
  // a narrow viewport (see ui/table.tsx) — announced only when it overflows.
  'cost.byModelScrollLabel': 'Spend by model, scroll horizontally for more columns',
  'cost.byProjectScrollLabel': 'Spend by project, scroll horizontally for more columns',
  'cost.windowSelectorLabel': 'Time window',
  'cost.windowDays': '{days}d',
  'cost.byModelTitle': 'Spend by model',
  'cost.byModelDescription': 'Cost, share of total, and token volume per model',
  'cost.noByModel': 'No per-model cost data yet.',
  'cost.byProjectTitle': 'Spend by project',
  'cost.byProjectDescription': 'Cost and share of total per project',
  'cost.noByProject': 'No per-project cost data yet.',
  'cost.percentOfTotal': '% of total',
  'cost.unpricedBanner':
    '{count} model(s) have no pricing data on record — total cost is understated: {models}',

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
    "Graph-native views of HivePilot's own state. Pan and zoom the canvas; select a node for its detail.",
  'graph.loadingSources': 'Loading sources…',
  'graph.noSources': 'No graph sources registered.',
  'graph.source': 'Source',
  'graph.loadingGraph': 'Loading graph…',
  'graph.requiresTokenLead': 'This source requires a',
  'graph.requiresTokenTail': 'token.',
  'graph.requiresTokenNote':
    'Your current token can still use the other Pollen tabs — only this graph source needs a higher role.',
  'graph.higherPrivilege': 'higher-privilege',
  'graph.chooseParam': 'Choose a {param}…',
  'graph.missingParamTitle': 'Pick a {params} to draw',
  'graph.missingParamBodySelect':
    'This source draws one at a time. Choose one from the selector above and the graph loads immediately.',
  'graph.missingParamBodyType':
    'This source cannot list its accepted values, so type one above and click Load.',
  'graph.failedToLoad': 'Failed to load this graph ({label}). Try again or choose a different source.',
  'graph.noNodes': 'This source has no nodes yet.',
  'graph.selectNodeForDetail': 'Select a node for detail.',
  'graph.loadingDetail': 'Loading detail…',
  'graph.nodeRequiresTokenLead': "This node's detail requires a",
  'graph.nodeRequiresTokenTail': 'token.',
  'graph.colorBy': 'Color by',
  'graph.colorByStatus': 'Status',
  'graph.colorByKind': 'Kind',
  'graph.colorByRole': 'Role',
  'graph.run': 'Run',
  'graph.latestRun': 'Latest run',
  'graph.live': 'Live',
  'graph.reload': 'Reload',
  'graph.canvasHint': 'drag nodes to arrange · scroll to zoom',
  'graph.statusSuccess': 'success',
  'graph.statusRunning': 'running',
  'graph.statusSkipped': 'skipped',
  'graph.statusFailed': 'failed',

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
    'Your current token can still use the other Pollen tabs — only Mem0 search needs a higher role.',
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
  // Placeholders are ONLY reached by the free-text fallback in
  // `NewRunDrawer` (when a catalogue endpoint is unavailable or genuinely
  // empty). The normal path is a pick-list of server-known values.
  'runs.taskPlaceholder': 'Task name',
  'runs.projectPlaceholder': 'Project name',
  'runs.chooseTask': 'Choose a task…',
  'runs.chooseProject': 'Choose a project…',
  'runs.taskCatalogueUnavailable': 'Task list unavailable — type the name as declared in your config.',
  'runs.projectCatalogueUnavailable':
    'Project list unavailable — type the name as declared in your config.',
  'runs.newRunTitle': 'New run',
  'runs.newRunHelp': 'Pick a project and a task. The run starts immediately and appears on the board.',
  'runs.newRunCloseAriaLabel': 'Close the new run form',
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

  // ---- Run Board view (Mirador Operate section — Kanban of runs) -------
  'board.description': 'Live status of every run, grouped by stage — click a card for detail.',
  'board.descriptionReadOnly':
    'Live status of every run, grouped by stage (read-only — a run-rank token can trigger new ones).',
  'board.noRunsTitle': 'No runs yet',
  'board.noRunsBody':
    'Every pipeline you trigger shows up here, grouped by stage, and updates itself every few seconds. Start one to fill the board.',
  'board.noRunsBodyReadOnly':
    'Every pipeline triggered on this tenant shows up here, grouped by stage. Nothing has run yet.',
  'board.noMatchTitle': 'No run matches these filters',
  'board.noMatchBody': 'There are runs on the board, just none for this project/task combination.',
  'board.clearFilters': 'Clear filters',
  'board.allProjects': 'All projects',
  'board.allTasks': 'All tasks',
  'board.density': 'Density',
  'board.densityComfortable': 'Comfortable',
  'board.densityCompact': 'Compact',
  'board.showingCount': '{shown} of {total} runs',
  // Failure/pause reasons, derived from the canonical run status — the only
  // real "why" the list endpoint carries (`detail` is untrusted free text
  // and is never rendered).
  'board.reasonFailed': 'The pipeline reported a failure.',
  'board.reasonDenied': 'An approver denied this run.',
  'board.reasonRateLimit': 'Stopped by a provider rate limit.',
  'board.reasonAuthExpired': 'Provider credentials expired.',
  'board.reasonTestFailure': 'Tests failed.',
  'board.reasonSecurityBlocker': 'Blocked by a security gate.',
  'board.reasonCancelled': 'Stopped by an operator.',
  'board.reasonPaused': 'Paused mid-run — waiting to be resumed.',
  'board.reasonDeferred': 'Deferred — will retry later.',
  'board.colQueued': 'Queued',
  'board.colRunning': 'Running',
  'board.colWaitingApproval': 'Waiting approval',
  'board.colFailed': 'Failed',
  'board.colDone': 'Done',
  'board.colOther': 'Other',
  'board.cardAriaLabel': 'View details for run {id} ({task} on {project})',
  'board.listToggleLabel': 'Toggle list view',
  'board.startedAgo': 'started {age} ago',
  'board.duration': 'ran for {duration}',
  'board.kanbanScrollLabel': 'Scroll board columns horizontally',

  // ---- Run detail drill-down panel (Mirador Operate section) -----------
  'runDetail.title': 'Run #{id}',
  'runDetail.closeAriaLabel': 'Close run detail',
  'runDetail.stepsTitle': 'Steps',
  'runDetail.noSteps': 'No step detail recorded for this run.',
  'runDetail.overallDetail': 'Detail',
  'runDetail.provider': 'Provider',
  'runDetail.model': 'Model',
  'runDetail.tokens': 'Tokens (in/out)',
  'runDetail.cost': 'Cost',
  'runDetail.loadFailed': 'Failed to load run detail.',
  'runDetail.requiresTokenLead': 'Run detail requires a',
  'runDetail.requiresTokenTail': '(or higher) token.',

  // ---- Memory quality view (memory-quality dashboard) ------------------------
  'quality.kpiTitle': 'Memory quality',
  'quality.searchSuccessRate': 'Search success rate',
  'quality.noResultSearches': 'No-result searches',
  'quality.avgFreshness': 'Avg. recall freshness',
  'quality.declaredReliability': 'Declared reliability',
  'quality.onNSearches': 'on {count} searches',
  'quality.onNEvaluations': 'on {count} evaluations',
  'quality.noSamples': 'No data',
  'quality.noKpiData': 'No searches or evaluations recorded in this window.',
  'quality.gapsTitle': 'Gaps by namespace',
  'quality.gapsDescription': 'No-result searches grouped by namespace, most gaps first',
  'quality.noGaps': 'No search gaps recorded.',
  'quality.topQueriesLabel': 'top queries:',
  'quality.evaluationsTitle': 'Recent evaluations',
  'quality.evaluationsDescription': 'Human "was this memory useful?" feedback',
  'quality.noEvaluations': 'No evaluations recorded yet.',
  'quality.useful': 'Useful',
  'quality.notUseful': 'Not useful',
  'quality.journalTitle': 'Activity journal',
  'quality.journalDescription': 'Most recent memory events (search / read / store), most recent first',
  'quality.noJournal': 'No memory activity recorded yet.',
  'quality.colTs': 'Time',
  'quality.colOp': 'Operation',
  'quality.colNamespace': 'Namespace',
  'quality.colQuery': 'Query / key',
  'quality.colResult': 'Result',
  'quality.colFreshness': 'Freshness',
  'quality.colActor': 'Actor',
  'quality.emptyTitle': 'No memory activity recorded yet',
  'quality.emptyState':
    'These figures come from mem0 instrumentation, which is opt-in. Once it is enabled and agents start searching and storing memory, search success, recall freshness and the gaps by namespace appear here.',
  'quality.requiresTokenLead': 'This section requires a',
  'quality.requiresTokenTail': 'higher-privilege token.',
  'quality.requiresTokenNote':
    'Your current token can still use the other Pollen tabs — only this section needs a higher role.',

  // ---- Memory view (unified Quality/Growth/Search tabs) -----------------
  'memory.description':
    'Whether the memory substrate actually helps (Quality), how much of it there is (Growth), and what is in it (Search).',
  'memory.tabQuality': 'Quality',
  'memory.tabGrowth': 'Growth',
  'memory.tabSearch': 'Search',
  'memory.growthTitle': 'Memory growth',
  'memory.growthDescription': 'How much is stored, where, over time, and by whom.',
  'memory.totalMemories': 'Total memories',
  'memory.byNamespaceTitle': 'Memories by namespace',
  'memory.growthOverTimeTitle': 'Growth over time',
  'memory.noGrowthSeries': 'No growth recorded in this window.',
  'memory.byActorTitle': 'By actor',
  'memory.authorshipNotAvailable': 'A human-vs-agent authorship split is not available — showing the real by-actor breakdown instead.',
  'memory.growthEmptyTitle': 'Nothing stored in this window',
  'memory.growthEmptyState':
    'The namespace and per-actor breakdowns fill in as agents store memories. Nothing has been written in the last 30 days.',

  // ---- Home view (default landing view) ---------------------------------
  'home.subtitle': 'Your fleet at a glance — click any figure to dig in.',
  // The numbered "01 Snapshot" section header above the hero KPI grid (see
  // `SectionHeader`).
  'home.kpiSectionTitle': 'Snapshot',
  'home.refreshingLabel': 'Refreshing',
  'home.kpiSpendToday': 'Spend today',
  'home.kpiSpendSub': 'last 24h',
  'home.kpiTokensSaved': 'Tokens saved',
  'home.kpiTokensSavedSub': 'headroom + rtk, combined',
  'home.kpiRunsSuccess': 'Runs · success rate',
  'home.kpiRunsSub': 'last 24h',
  'home.kpiMemoryHealth': 'Memory health',
  // Bug fix (KPI row uniformity): the Memory Health card used to be a
  // bespoke centered Gauge layout instead of the standard
  // icon+label+value+sub structure every other hero KPI uses — this sub
  // line is that card's `sub` slot once it's a normal `MetricReadout`.
  'home.kpiMemorySub': 'search success rate',
  'home.kpiPendingApprovals': 'Pending approvals',
  // Bug fix (KPI row uniformity): every hero KPI card now always has a sub
  // line (Approvals used to be the only one without one, making its card
  // visibly shorter than its siblings).
  'home.kpiApprovalsSub': 'awaiting review',
  'home.kpiRequiresRole': 'Requires a higher-role token',
  'home.notAvailable': 'Not available',
  'home.noData': 'No data',
  'home.needsAttentionTitle': 'Needs attention',
  'home.needsAttentionDescription': 'Oldest pending approvals first, then recent failed runs.',
  'home.allClear': 'All clear — nothing needs your attention right now.',
  'home.attentionApprovalBadge': 'Approval',
  'home.attentionFailedRunBadge': 'Failed run',
  'home.ageAgo': '{age} ago',
  'home.needsAttentionForbidden': 'Approvals and runs require a run-rank (or higher) token to preview here.',
  'home.sweepTitle': 'The Sweep',
  'home.sweepDescription': 'Live status of running and recent pipeline runs.',
  'home.sweepEmpty': 'No active or recent runs yet.',
  'home.sweepLegendRunning': 'Running',
  'home.sweepLegendWaiting': 'Awaiting approval',
  'home.sweepLegendFailed': 'Failed',
  'home.sweepLegendIdle': 'Idle / other',
  'home.activityFeedTitle': 'Activity feed',
  'home.activityFeedDescription': 'Most recent runs and approvals, newest first.',
  'home.activityFeedEmpty': 'No activity yet.',
  'home.activityRunLabel': 'Run',
  'home.activityApprovalLabel': 'Approval',

  // ---- Models view (Mirador Spend section) ------------------------------
  'models.title': 'Models',
  'models.tableScrollLabel': 'Models table, scroll horizontally for more columns',
  'models.description': 'Per-model cost, token volume, and success rate',
  'models.noModels': 'No model data yet.',
  'models.costPerSuccessfulRun': 'Cost per successful run',
  'models.costPerSuccessfulRunSub': 'total cost / succeeded runs',
  'models.noSucceededRuns': 'No succeeded runs yet',
  'models.shareOfSpendTitle': 'Share of spend',
  'models.shareOfSpendDescription': 'Cost distribution across models',
  'models.successRate': 'Success rate',
  'models.noAttempts': 'No attempts',
  'models.latencyTitle': 'Latency',
  'models.latencyNotAvailable':
    'Not available — p50/p95 latency cannot be computed from current data.',

  // ---- Efficiency view (Mirador Spend section) ---------------------------
  'efficiency.title': 'Efficiency',
  'efficiency.description': 'Token-savings signals from Headroom compression and the rtk CLI',
  'efficiency.headroomTitle': 'Headroom',
  'efficiency.headroomDescription': 'Context-compression savings recorded by the Headroom plugin',
  'efficiency.headroomNotAvailable': 'Headroom is not reporting yet — no compressions have been recorded.',
  'efficiency.tokensSaved': 'Tokens saved',
  'efficiency.compressions': 'Compressions recorded',
  'efficiency.charsSaved': 'Characters saved',
  'efficiency.avgCompressionRate': 'Avg compression rate',
  'efficiency.p95Ratio': 'P95 ratio (worst case)',
  'efficiency.p95RatioSub': 'of original size kept',
  'efficiency.rtkTitle': 'rtk',
  'efficiency.rtkDescription': 'Global command-level token savings (rtk CLI), not tenant-scoped',
  'efficiency.rtkNotAvailable': 'rtk not available on this host',
  'efficiency.rtkGain': 'rtk gain',
  'efficiency.rtkTokensSaved': 'Tokens saved (rtk)',
  'efficiency.rtkCommands': 'Commands tracked',
  'efficiency.rtkSavedSeriesTitle': 'Savings trend',
  'efficiency.noSavedSeries': 'No daily series recorded yet.',

  // ---- Autopilot view ---------------------------------------------------
  'autopilot.description': 'The guarded objective queue — pause, resume, and monitor what it dispatches.',
  'autopilot.statusLabel': 'Status',
  'autopilot.active': 'Active',
  'autopilot.paused': 'Paused',
  'autopilot.queueDepthLabel': 'Queue depth',
  'autopilot.pauseButton': 'Pause',
  'autopilot.resumeButton': 'Resume',
  'autopilot.pauseConfirm': 'Pause autopilot? It will stop dispatching new objectives until resumed.',
  'autopilot.resumeConfirm': 'Resume autopilot? It will start dispatching queued objectives again.',
  'autopilot.insufficientRole': 'Insufficient role — your token can no longer pause/resume autopilot.',
  'autopilot.controlRequiresRunRole': 'Requires a run-rank (or higher) token to pause/resume.',
  'autopilot.forbidden': "Unable to load Autopilot state for your token's tenant.",
  'autopilot.budgetTitle': 'Budget',
  'autopilot.dailyBudget': 'Daily budget',
  'autopilot.spentToday': 'Spent today',
  'autopilot.remaining': 'Remaining',
  'autopilot.unknown': 'unknown',
  'autopilot.noBudgetTitle': 'No daily spend ceiling',
  'autopilot.noBudgetBody':
    'Set budget_daily_usd in policies.yaml to cap what autopilot may spend in a day. Without it, dispatches are not budget-gated.',
  'autopilot.budgetBurn': 'Budget burn',
  'autopilot.queueTitle': 'Objective queue',
  'autopilot.queueEmptyTitle': 'Nothing queued',
  'autopilot.queueEmptyBody':
    'Objectives land here when a scheduled pipeline or a drift scan raises one. Autopilot drains at most one per tick.',
  'autopilot.enqueuedAgo': 'enqueued {age} ago',
  'autopilot.dispatchesTitle': 'Recent dispatches',
  'autopilot.dispatchesEmptyTitle': 'Nothing dispatched yet',
  'autopilot.dispatchesEmptyBody':
    'A dispatch is recorded each time autopilot drains an objective from the queue — only for pipelines on the allowlist below.',
  'autopilot.allowlistTitle': 'Allowlisted pipelines',
  'autopilot.allowlistEmptyTitle': 'No pipeline may auto-dispatch',
  'autopilot.allowlistEmptyBody':
    'Autopilot can still queue objectives, but it will never run one. Add a pipeline to auto_dispatch in policies.yaml to let it act.',

  // ---- Partitions view (propose -> ratify -> dispatch PRD, Sprint 4) ----
  // Register note: sober and literal. Nothing here dresses up what is, in
  // substance, "you are about to start N agents and push code outward".
  'partitions.description':
    'A proposed partition splits one piece of work into budgeted tasks. Review the plan, edit it, then dispatch. Nothing runs until you ratify.',
  'partitions.descriptionReadOnly':
    'Read-only. Ratifying a partition — the single gate between a proposal and N running agents — needs an approve-rank token.',
  'partitions.forbidden':
    'This list needs a run-rank (or higher) token. Your token still works on every other Pollen tab.',
  'partitions.emptyTitle': 'No partitions yet',
  'partitions.emptyBody':
    'A partition appears here once a proposer pipeline submits one (hivepilot partition submit --file plan.json --source text:docs/bug-1234.md). Who the proposer is, and what a task means for you, is your config — not the engine.',
  'partitions.review': 'Review',
  'partitions.reviewAriaLabel': 'Review partition {id}',
  'partitions.sourceLabel': 'Source',
  'partitions.proposedAgo': 'proposed {age} ago',
  'partitions.notRatifiable': 'Only a proposed partition can be ratified. This one is {status}.',

  // ---- ratification drawer --------------------------------------------
  'partitions.drawerTitle': 'Ratify partition',
  'partitions.drawerAriaLabel': 'Ratify partition {id}',
  // One panel serves both the ratification form and the journal, so the
  // close control is named after the panel, not after one of its two jobs.
  'partitions.closeAriaLabel': 'Close the partition panel',
  'partitions.planTitle': 'Plan',
  'partitions.planLabel': 'Partition plan (JSON)',
  'partitions.planHint':
    'This is exactly what will run. Edit it directly, or use the task controls below — both write to the same document.',
  'partitions.parseErrorLead': 'This is not valid JSON, so nothing can be dispatched:',
  'partitions.checking': 'Checking the plan…',
  'partitions.gateAccepted': 'The gate accepts this plan.',
  'partitions.gateRefusedLead': 'The gate would refuse this plan:',
  'partitions.previewUnavailable':
    'The plan could not be checked against policy right now, so dispatch stays disabled.',

  // ---- typed controls --------------------------------------------------
  'partitions.tasksTitle': 'Tasks',
  'partitions.noTasks': 'This plan declares no tasks',
  'partitions.noTasksBody':
    'A partition with no tasks dispatches nothing. Add a task to the JSON above, or reload the proposal.',
  'partitions.dropTask': 'Drop',
  'partitions.dropTaskAriaLabel': 'Drop task {id} from this plan',
  'partitions.dependsOn': 'after {ids}',
  'partitions.wallClockLabel': 'Killed after',
  'partitions.wallClockAriaLabel': 'Wall-clock ceiling in seconds for task {id}',
  'partitions.wallClockHelp':
    'An enforcement ceiling, not an estimate: the task is killed at this point.',
  'partitions.costLabel': 'Cost ceiling ($)',
  'partitions.costAriaLabel': 'Cost ceiling in US dollars for task {id}',
  'partitions.costHelp':
    'Admission control: the sum is checked against the remaining daily budget. It is a pre-check, not a reservation — one wave can overshoot it.',
  'partitions.planCostLabel': 'Plan cost ceiling',
  'partitions.wavesLabel': 'Waves',
  'partitions.waveNumber': 'Wave {index}',

  // ---- outward consent -------------------------------------------------
  'partitions.outwardTitle': 'Outward-visible action',
  // The sentence the checkbox is asking about. `{actions}` is built from the
  // action list the BACKEND computed for this exact plan — never a fixed
  // string, and never the plan's own self-declared `outward` flags.
  'partitions.outwardWarning': '{actions} — an action visible outside this machine.',
  'partitions.outwardConsentLabel': 'I consent to these outward-visible actions',
  'partitions.outwardNoneTitle': 'Nothing outward',
  'partitions.outwardNoneBody':
    'On live config, this plan pushes nothing and opens nothing. Work stays on this machine, so no outward consent is required.',
  'partitions.outwardV1Gap':
    'Consent is enforced at dispatch for git and forge actions only. notify, vault_write and external_api are named above but are not yet suppressed at runtime.',
  'partitions.outwardHonesty':
    'This governs what the engine does. An agent with shell access can still act outward on its own.',
  'partitions.outwardAction.git_push': 'branches will be pushed',
  'partitions.outwardAction.forge_pr': 'PRs opened',
  'partitions.outwardAction.forge_merge': 'PRs merged',
  'partitions.outwardAction.forge_issue': 'issues opened',
  'partitions.outwardAction.forge_release': 'releases published',
  'partitions.outwardAction.notify': 'notifications sent out',
  'partitions.outwardAction.vault_write': 'notes written to your vault',
  'partitions.outwardAction.external_api': 'external APIs called',

  // ---- effective parallelism ------------------------------------------
  'partitions.parallelismLabel': 'Effective parallelism',
  'partitions.parallelismSub': '{requested} requested',
  'partitions.parallelismTitle': 'What actually runs in parallel',

  // ---- dispatch --------------------------------------------------------
  'partitions.dispatch': 'Ratify and dispatch',
  'partitions.dispatchAriaLabel': 'Ratify and dispatch partition {id}',
  'partitions.dispatchConfirm':
    'Dispatch {count} task(s) now? This starts real agents, at most {effective} at a time. A ratified partition cannot be edited afterwards.',
  'partitions.dispatchBlocked': 'Dispatch stays disabled until the plan parses and the gate accepts it.',
  'partitions.dispatched': 'Ratified. {count} task(s) queued — dispatch is running in the background.',
  'partitions.idempotent': 'Already ratified. Nothing was dispatched a second time.',
  'partitions.warningsTitle': 'Warnings',
  'partitions.insufficientRole': 'Your token cannot ratify a partition — this needs an approve-rank token.',
  'partitions.errorStale':
    'This partition changed since you opened it. Reload it and review the new plan before ratifying.',

  // ---- dispatch journal (Sprint 5) -------------------------------------
  // The journal is a record, not a status page: it says what happened, who
  // caused it, what it cost, and what came out of it. Where the engine does
  // not know something it says so — an em-dash for "not recorded", the word
  // "unknown" for "recorded as unmeasurable". Never a zero, never a guess.
  'partitions.history': 'Journal',
  'partitions.historyAriaLabel': 'Open the dispatch journal for partition {id}',
  'partitions.journalTitle': 'Dispatch journal',
  'partitions.journalDrawerTitle': 'Partition journal',
  'partitions.journalDrawerAriaLabel': 'Dispatch journal for partition {id}',
  'partitions.journalScrollLabel':
    'Dispatch journal — scroll sideways for the remaining columns',
  'partitions.journalEmptyTitle': 'Nothing dispatched yet',
  'partitions.journalEmptyBody':
    'One row lands here per task the moment this partition is ratified and its first wave is claimed. A proposed partition has nothing to report yet.',
  'partitions.colTask': 'Task',
  'partitions.colStatus': 'Status',
  'partitions.colActor': 'Actor',
  'partitions.colClaimed': 'Claimed',
  'partitions.colPr': 'PR',
  'partitions.colCost': 'Cost',
  'partitions.colAttempt': 'Attempt',
  'partitions.costUnknown': 'unknown',
  'partitions.costUnknownTitle':
    'This task reached a terminal state but no step reported a cost. The spend is unknown, not zero.',
  'partitions.prNoneTitle': 'No pull request URL was recorded for this task.',
  'partitions.prNotWebTitle':
    'Recorded verbatim: this value is not an http(s) URL, so it is shown as text and not turned into a link.',
  'partitions.prAriaLabel': 'Open the pull request recorded for task {id}',
  'partitions.journalPrNote':
    'A task with no PR link shows —. The engine attributes a URL only when exactly one pull request was opened inside that task’s window, so two tasks running against the same project at once are both recorded as —. A missing link is a gap; a wrong link would be a lie.',
  'partitions.journalSkippedNote':
    'skipped means the task never ran because a prerequisite failed — deliberately recorded as skipped and not as failed.',

  // ---- Agents view (Mirador Agent Panels backend sprint frontend) ------
  'agents.title': 'Agents',
  'agents.description': 'What each role costs and how often it succeeds. Select a role for its lessons and verdicts.',
  'agents.colRole': 'Role',
  'agents.tableScrollLabel': 'Agents table, scroll horizontally for more columns',
  'agents.rowAriaLabel': 'Open detail for {name}',
  'agents.attentionTitle': '{count} role(s) need attention',
  'agents.allClear': 'All roles nominal — no failing verdicts, no low success rate.',
  'agents.reasonVerdict': 'recent verdict was not an accept',
  'agents.reasonLowSuccess': 'success rate {rate}%',
  'agents.noRoster': 'No agent roster configured',
  'agents.noRosterBody':
    'Roles are declared in your config (roles.yaml). Once a role exists and a run attributes a step to it, its cost and success rate appear here.',
  'agents.forbidden': "Unable to load agent activity for your token's tenant.",
  'agents.verdictsForbidden': "Unable to load verdict severity signals for your token's tenant.",
  'agents.noActivityYet': 'No activity attributed yet.',
  'agents.noAttemptsYet': 'No attempts yet',
  'agents.attentionBadge': 'Needs attention',
  'agents.costLabel': 'Cost',
  'agents.runsLabel': 'Runs',
  'agents.stepsLabel': 'Steps',
  'agents.tokensLabel': 'Tokens (in/out)',
  'agents.lastActiveLabel': 'Last active',
  'agents.successRateLabel': 'Success rate',
  'agents.unknownTitle': 'Unattributed activity',
  'agents.unknownDescription':
    'Recorded before per-role attribution existed. It cannot be assigned to a role, so it is excluded from every figure above.',
  'agents.detailAriaLabel': 'Agent detail: {name}',
  'agents.closeAriaLabel': 'Close agent detail',
  'agents.lessonsTitle': 'Recent lessons',
  'agents.noLessons': 'No lessons recorded for this role yet.',
  'agents.verdictsTitle': 'Recent verdicts',
  'agents.noVerdicts': 'No verdicts recorded for this role yet.',
  'agents.validated': 'Validated',
  'agents.candidate': 'Candidate',
  'agents.scoreLabel': 'score {score}',
  'agents.confidenceLabel': '{confidence}% confidence',
  'agents.unknownKind': 'unknown',
  'agents.noDecision': 'No confident decision',
} as const

/** Every valid translation key — derived from `en`, the single source of
 * truth for the dictionary's key set. `fr.ts` is typed against this. */
export type TranslationKey = keyof typeof en
