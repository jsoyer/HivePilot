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

  // ---- header / shell ----------------------------------------------------
  'header.subtitle': 'Tableau de bord HivePilot',

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
}
