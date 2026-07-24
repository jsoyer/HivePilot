import type { TranslationKey } from './en'

/**
 * French dictionary — typed `Record<TranslationKey, string>` against `en.ts`
 * so a missing or extra key is a COMPILE error, not a silent runtime gap.
 * The operator is a native FR speaker (Mirador -> "Vigie" upgrade, P1a) —
 * natural French copy, not a literal word-for-word translation. Product/
 * technical proper nouns ("Mirador", "Mem0") are left untranslated.
 */
export const fr: Record<TranslationKey, string> = {
  // ---- common ----------------------------------------------------------
  'common.load': 'Charger',
  'common.enable': 'Activer',
  'common.disable': 'Désactiver',
  'common.working': 'En cours…',
  'common.openNavigation': 'Ouvrir la navigation',
  'common.expandSidebar': 'Déplier la barre latérale',
  'common.collapseSidebar': 'Réduire la barre latérale',
  'common.switchToLightTheme': 'Passer au thème clair',
  'common.switchToDarkTheme': 'Passer au thème sombre',
  'common.switchToEnglish': 'Passer en anglais',
  'common.switchToFrench': 'Passer en français',
  'common.lastDays': 'Derniers {days} jours',
  'common.lastDaysLower': 'derniers {days} jours',
  'common.and': ' et ',
  'common.loading': 'Chargement…',
  'common.noDataYet': 'Aucune donnée pour le moment.',
  'common.project': 'Projet',
  'common.task': 'Tâche',
  'common.status': 'Statut',
  'common.run': 'Exécution',
  'common.actions': 'Actions',
  'common.processing': 'Traitement en cours…',
  'common.starting': 'Démarrage…',
  'common.stopping': 'Arrêt en cours…',
  'common.requiresRunRankLead': 'Cette vue nécessite un jeton de rang',
  'common.requiresRunRankTail':
    '(ou supérieur). Votre jeton actuel fonctionne toujours pour les autres onglets de Mirador — seule cette liste nécessite un rôle supérieur.',

  // ---- header / shell ----------------------------------------------------
  'header.subtitle': 'Tableau de bord HivePilot',
  'header.search': 'Rechercher',

  // ---- command palette (P1b: Cmd+K / Ctrl+K) ----------------------------
  'palette.title': 'Palette de commandes',
  'palette.placeholder': 'Rechercher des vues et actions…',
  'palette.noResults': 'Aucune commande correspondante.',
  'palette.actionsGroup': 'Actions',
  'palette.toggleTheme': 'Changer de thème (clair/sombre)',
  'palette.toggleLanguage': 'Changer de langue (EN/FR)',

  // ---- nav -----------------------------------------------------------
  'nav.overview': "Vue d'ensemble",
  'nav.agents': 'Agents',
  'nav.system': 'Système',
  'nav.memory': 'Mémoire',
  'nav.panels': 'Panneaux',
  'nav.analytics': 'Analytique',
  'nav.cost': 'Coûts',
  'nav.health': 'Santé',
  'nav.mem0': 'Mem0',
  'nav.approvals': 'Approbations',
  'nav.runs': 'Exécutions',
  'nav.graph': 'Graphe',

  // ---- health status words (shared: header pills + Health tab badges) --
  'health.status.ok': 'ok',
  'health.status.degraded': 'dégradé',
  'health.status.error': 'erreur',

  // ---- Analytics view ----------------------------------------------------
  'analytics.volumeTitle': 'Volume et résultats',
  'analytics.noRuns': 'Aucune exécution enregistrée sur cette période.',
  'analytics.totalRuns': 'Exécutions totales',
  'analytics.succeeded': 'Réussies',
  'analytics.runsCount': '{count} exécutions',
  'analytics.failed': 'Échouées',
  'analytics.noAttempts': '{count} ignorées, aucune tentative',
  'analytics.other': 'Autres',
  'analytics.trendTitle': 'Tendance',
  'analytics.trendDescription': 'Exécutions par jour',
  'analytics.noTrend': 'Aucune donnée de tendance pour cette période.',
  'analytics.durationTitle': 'Percentiles de durée',
  'analytics.durationDescription': 'Exécutions terminées, p50 / p95 / p99',
  'analytics.noDuration': "Aucune exécution terminée pour l'instant.",
  'analytics.hotspotsTitle': "Points chauds d'échec des étapes",
  'analytics.hotspotsDescription': "Étapes avec le plus d'échecs en premier",
  'analytics.noHotspots': "Aucun échec d'étape enregistré.",
  'analytics.step': 'Étape',
  'analytics.status': 'Statut',
  'analytics.count': 'Nombre',
  'analytics.approvalLatencyTitle': "Latence d'approbation",
  'analytics.approvalLatencyDescription': 'Temps entre la demande et la décision',
  'analytics.noApprovals': "Aucune approbation traitée pour l'instant.",
  'analytics.actionedApprovals': 'Approbations traitées',

  // ---- Cost view -----------------------------------------------------
  'cost.title': 'Coûts et tokens',
  'cost.noCost': "Aucune donnée de coût pour l'instant.",
  'cost.totalCost': 'Coût total',
  'cost.inputTokens': "Tokens d'entrée",
  'cost.outputTokens': 'Tokens de sortie',
  'cost.unpricedSteps': 'Étapes non tarifées',
  'cost.provider': 'Fournisseur',
  'cost.steps': 'Étapes',
  'cost.tokensInOut': 'Tokens (entrée/sortie)',
  'cost.costLabel': 'Coût',
  'cost.unpriced': 'Non tarifié',
  'cost.model': 'Modèle',
  'cost.providerVolumeTitle': 'Volume par fournisseur et modèle',
  'cost.providerVolumeDescription': "Nombre d'étapes et répartition des résultats",
  'cost.noProviderData': "Aucune donnée de fournisseur/modèle pour l'instant.",
  'cost.total': 'Total',
  'cost.succeeded': 'Réussies',
  'cost.failed': 'Échouées',

  // ---- Health view -----------------------------------------------------
  'health.title': 'État des plugins',
  'health.description': "État global des plugins, identique à `hivepilot plugins health`.",
  'health.restartNote': " Activer/désactiver ne s'applique qu'au prochain redémarrage du serveur.",
  'health.noPlugins': 'Aucun plugin enregistré.',
  'health.disabledPlugins': 'Plugins désactivés',
  'health.disablePending': 'désactivation en attente · redémarrage',
  'health.disabled': 'désactivé',
  'health.insufficientRole': 'Rôle insuffisant — votre jeton ne peut plus activer/désactiver les plugins.',
  'health.restartRequired': 'redémarrage requis',
  'health.restartTakesEffectTitle':
    'Prend effet uniquement au prochain redémarrage — pas de rechargement à chaud.',
  'health.pendingBadgeTitle':
    'Marqué pour désactivation — prendra effet au prochain redémarrage du serveur. Toujours actif actuellement.',
  'health.restartAppliesTitle': "Ce changement s'applique au prochain redémarrage du serveur API.",

  // ---- Graph view ------------------------------------------------------
  'graph.title': 'Graphe',
  'graph.description':
    "Vues natives en graphe de l'état de HivePilot — déplacez/zoomez le canevas, cliquez sur un nœud pour le détail.",
  'graph.loadingSources': 'Chargement des sources…',
  'graph.noSources': 'Aucune source de graphe enregistrée.',
  'graph.source': 'Source',
  'graph.loadingGraph': 'Chargement du graphe…',
  'graph.requiresTokenLead': 'Cette source nécessite un rôle',
  'graph.requiresTokenTail': '.',
  'graph.requiresTokenNote':
    'Votre jeton actuel fonctionne toujours pour les autres onglets de Mirador — seule cette source de graphe nécessite un rôle supérieur.',
  'graph.higherPrivilege': 'à privilège supérieur',
  'graph.aParameter': 'un paramètre',
  'graph.parameters': 'des paramètres',
  'graph.missingParamHintLead': 'Cette source nécessite {countLabel} pour charger les données. Renseignez',
  'graph.missingParamHintTail': 'ci-dessus puis cliquez sur',
  'graph.failedToLoad': 'Échec du chargement de ce graphe ({label}). Réessayez ou choisissez une autre source.',
  'graph.noNodes': "Cette source n'a encore aucun nœud.",
  'graph.selectNodeForDetail': 'Sélectionnez un nœud pour voir le détail.',
  'graph.loadingDetail': 'Chargement du détail…',
  'graph.nodeRequiresTokenLead': 'Le détail de ce nœud nécessite un rôle',
  'graph.nodeRequiresTokenTail': '.',

  // ---- Mem0 view ---------------------------------------------------------
  'mem0.title': 'Recherche de mémoire Mem0',
  'mem0.description': 'Recherche sémantique dans le magasin mem0 — nécessite un jeton admin',
  'mem0.searchPlaceholder': 'Rechercher des mémoires…',
  'mem0.searchAriaLabel': 'Rechercher des mémoires',
  'mem0.searchButton': 'Rechercher',
  'mem0.searchHint': 'Saisissez une recherche ci-dessus pour consulter les mémoires.',
  'mem0.requiresTokenLead': 'Cette vue nécessite un rôle',
  'mem0.requiresTokenTail': '.',
  'mem0.requiresTokenNote':
    'Votre jeton actuel fonctionne toujours pour les autres onglets de Mirador — seule la recherche Mem0 nécessite un rôle supérieur.',
  'mem0.notConfigured': "mem0 n'est pas configuré.",
  'mem0.noResults': 'Aucune mémoire trouvée pour cette recherche.',
  'mem0.category': 'Catégorie',
  'mem0.timestamp': 'Horodatage',
  'mem0.memory': 'Mémoire',

  // ---- Approvals view ------------------------------------------------------
  'approvals.descriptionCanApprove': 'Approbations de pipeline en attente — approuvez ou refusez ci-dessous.',
  'approvals.descriptionReadOnly':
    'Approbations de pipeline en attente (lecture seule — un jeton de rang approbateur peut agir dessus).',
  'approvals.noPending': 'Aucune approbation en attente.',
  'approvals.requested': 'Demandée',
  'approvals.approve': 'Approuver',
  'approvals.deny': 'Refuser',
  'approvals.approveAriaLabel': "Approuver l'exécution {id}",
  'approvals.denyAriaLabel': "Refuser l'exécution {id}",
  'approvals.denialReasonAriaLabel': "Motif de refus pour l'exécution {id}",
  'approvals.reasonPlaceholder': 'Motif du refus (obligatoire)…',
  'approvals.confirmDeny': 'Confirmer le refus',
  'approvals.insufficientRoleApprove':
    'Rôle insuffisant — votre jeton ne peut plus approuver/refuser cette exécution.',

  // ---- Runs view -----------------------------------------------------------
  'runs.descriptionCanRun': 'Déclenchez une nouvelle exécution et suivez son statut en direct.',
  'runs.descriptionReadOnly':
    'Exécutions récentes (lecture seule — un jeton de rang run peut en déclencher de nouvelles).',
  'runs.noRuns': 'Aucune exécution pour le moment.',
  'runs.taskPlaceholder': 'ex. deploy',
  'runs.projectPlaceholder': 'ex. acme-web',
  'runs.extraPromptLabel': 'Prompt supplémentaire (optionnel)',
  'runs.extraPromptPlaceholder': 'Contexte additionnel pour cette exécution…',
  'runs.autoGitLabel': 'Actions git automatiques (commit/push)',
  'runs.newRunButton': 'Nouvelle exécution',
  'runs.insufficientRoleCreate': "Rôle insuffisant — votre jeton ne peut plus déclencher d'exécutions.",
  'runs.stopButton': 'Arrêter',
  'runs.stopAriaLabel': "Arrêter l'exécution {id}",
  'runs.stopConfirm': "Arrêter l'exécution #{id} ({task} sur {project}) ?",
  'runs.insufficientRoleStop': 'Rôle insuffisant — votre jeton ne peut plus arrêter cette exécution.',
  'runs.started': 'Démarrée',
  'runs.finished': 'Terminée',
}
