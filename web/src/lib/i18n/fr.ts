import type { TranslationKey } from './en'

/**
 * French dictionary — typed `Record<TranslationKey, string>` against `en.ts`
 * so a missing or extra key is a COMPILE error, not a silent runtime gap.
 * The operator is a native FR speaker (Pollen dashboard upgrade, P1a) —
 * natural French copy, not a literal word-for-word translation. Product/
 * technical proper nouns ("Pollen", "Mem0") are left untranslated.
 */
export const fr: Record<TranslationKey, string> = {
  // ---- common ----------------------------------------------------------
  'common.load': 'Charger',
  'common.enable': 'Activer',
  'common.disable': 'Désactiver',
  'common.working': 'En cours…',
  'common.openNavigation': 'Ouvrir la navigation',
  'common.navigation': 'Navigation',
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
  'common.cancel': 'Annuler',
  'common.processing': 'Traitement en cours…',
  'common.starting': 'Démarrage…',
  'common.stopping': 'Arrêt en cours…',
  'common.requiresRunRankLead': 'Cette vue nécessite un jeton de rang',
  'common.requiresRunRankTail':
    '(ou supérieur). Votre jeton actuel fonctionne toujours pour les autres onglets de Pollen — seule cette liste nécessite un rôle supérieur.',

  // ---- header / shell ----------------------------------------------------
  'header.subtitle': 'tableau de bord HivePilot',
  'header.search': 'Rechercher',

  // ---- command palette (P1b: Cmd+K / Ctrl+K) ----------------------------
  'palette.title': 'Palette de commandes',
  'palette.placeholder': 'Rechercher des vues et actions…',
  'palette.noResults': 'Aucune commande correspondante.',
  'palette.actionsGroup': 'Actions',
  'palette.toggleTheme': 'Changer de thème (clair/sombre)',
  'palette.toggleLanguage': 'Changer de langue (EN/FR)',

  // ---- nav -----------------------------------------------------------
  'nav.atAGlance': "En un coup d'œil",
  'nav.home': 'Accueil',
  'nav.overview': "Vue d'ensemble",
  'nav.operate': 'Opérations',
  'nav.system': 'Système',
  'nav.memory': 'Mémoire',
  'nav.panels': 'Panneaux',
  'nav.analytics': 'Analytique',
  'nav.spend': 'Dépenses',
  'nav.cost': 'Coût',
  'nav.models': 'Modèles',
  'nav.efficiency': 'Efficacité',
  'nav.health': 'Santé',
  'nav.approvals': 'Approbations',
  'nav.runs': 'Exécutions',
  'nav.autopilot': 'Autopilote',
  'nav.partitions': 'Partitions',
  'nav.agents': 'Agents',
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
  'cost.sessionsTitle': 'Coût par session',
  'cost.sessionsDescription':
    "Où est passé l'argent, pas combien de tokens ont circulé. Les lectures de cache sont volumineuses et bon marché ; la sortie est petite et chère — lire les deux comme un seul chiffre dirige l'optimisation vers le mauvais paramètre.",
  'cost.noSessions': 'Aucune session chiffrée sur cette période.',
  'cost.sessionRun': 'Session',
  'cost.sessionTotal': 'Total',
  'cost.sessionOutput': 'Sortie',
  'cost.sessionInput': 'Entrée',
  'cost.sessionCacheRead': 'Lecture cache',
  'cost.sessionUnpriced': '{count} non chiffrées',
  'cost.title': 'Coûts et tokens',
  'cost.noCost': "Aucune donnée de coût pour l'instant.",
  'cost.totalCost': 'Coût total',
  'cost.inputTokens': "Tokens d'entrée",
  'cost.outputTokens': 'Tokens de sortie',
  'cost.unpricedSteps': 'Étapes non tarifées',
  'cost.steps': 'Étapes',
  'cost.tokensInOut': 'Tokens (entrée/sortie)',
  'cost.costLabel': 'Coût',
  'cost.model': 'Modèle',
  'cost.byModelScrollLabel': 'Dépenses par modèle, faites défiler horizontalement pour voir les autres colonnes',
  'cost.byProjectScrollLabel': 'Dépenses par projet, faites défiler horizontalement pour voir les autres colonnes',
  'cost.windowSelectorLabel': 'Période',
  'cost.windowDays': '{days}j',
  'cost.byModelTitle': 'Dépense par modèle',
  'cost.byModelDescription': "Coût, part du total et volume de tokens par modèle",
  'cost.noByModel': "Aucune donnée de coût par modèle pour l'instant.",
  'cost.byProjectTitle': 'Dépense par projet',
  'cost.byProjectDescription': 'Coût et part du total par projet',
  'cost.noByProject': "Aucune donnée de coût par projet pour l'instant.",
  'cost.percentOfTotal': '% du total',
  'cost.unpricedBanner':
    '{count} modèle(s) sans tarification connue — le coût total est sous-estimé : {models}',
  'cost.unpricedBannerUsage':
    "{count} modèle(s) ont des étapes non chiffrées faute d'usage enregistré — la table de prix est correcte, c'est la capture qui manque : {models}",

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
  // Activité — la seconde réponse, indépendante. Le statut dit « installé »,
  // l'activité dit « a réellement tourné ». `headroom` et `mem0` sont restés
  // à `ok` pendant des semaines en échouant à chaque appel : aucune de ces
  // formulations ne laisse un badge vert tenir lieu de preuve.
  'health.activityNote':
    " Le statut signifie installé et configuré. L'activité signifie qu'il a réellement tourné — les deux peuvent diverger pendant des semaines.",
  'health.summary.loaded': '{count} chargés',
  'health.summary.exercised': '{count} sollicités',
  'health.summary.idle': '{count} inactifs',
  'health.summary.unreadable': '{count} illisibles',
  'health.summary.neverRun': "{count} jamais exécutés",
  'health.summary.unmeasured': '{count} non mesurables',
  'health.activity.active': '{count} événements · dernier {when}',
  'health.activity.idle': 'rien depuis {days} j · dernier {when}',
  'health.activity.neverRun': 'jamais exécuté',
  'health.activity.unreadable': 'activité illisible',
  'health.activity.presenceOnly': 'simple test de présence',
  'health.activity.presenceOnlyTitle':
    "Ce plugin n'enregistre aucune télémétrie. Le badge de statut confirme qu'il est installé — pas qu'il fonctionne.",
  'health.activity.unreadableTitle':
    "Ce plugin est mesurable, mais la lecture de sa télémétrie a échoué. Ce n'est pas la même chose que n'avoir rien fait.",
  'health.activity.evidenceTitle': 'Mesuré depuis {evidence}.',
  'health.activity.contradiction': "annonce ok, n'a jamais tourné",
  'health.activity.contradictionTitle':
    "Le plugin se charge et est configuré, mais n'a aucune activité enregistrée. À vérifier avant de lui faire confiance.",

  // ---- Graph view ------------------------------------------------------
  'graph.title': 'Graphe',
  'graph.description':
    "Vues natives en graphe de l'état de HivePilot. Déplacez et zoomez le canevas ; sélectionnez un nœud pour son détail.",
  'graph.loadingSources': 'Chargement des sources…',
  'graph.noSources': 'Aucune source de graphe enregistrée.',
  'graph.source': 'Source',
  'graph.loadingGraph': 'Chargement du graphe…',
  'graph.requiresTokenLead': 'Cette source nécessite un rôle',
  'graph.requiresTokenTail': '.',
  'graph.requiresTokenNote':
    'Votre jeton actuel fonctionne toujours pour les autres onglets de Pollen — seule cette source de graphe nécessite un rôle supérieur.',
  'graph.higherPrivilege': 'à privilège supérieur',
  'graph.chooseParam': 'Choisir un {param}…',
  'graph.missingParamTitle': 'Choisissez un {params} à afficher',
  'graph.missingParamBodySelect':
    'Cette source en affiche un à la fois. Choisissez-en un dans le sélecteur ci-dessus et le graphe se charge aussitôt.',
  'graph.missingParamBodyType':
    'Cette source ne peut pas énumérer ses valeurs acceptées : saisissez-en une ci-dessus puis cliquez sur Charger.',
  'graph.failedToLoad': 'Échec du chargement de ce graphe ({label}). Réessayez ou choisissez une autre source.',
  'graph.noNodes': "Cette source n'a encore aucun nœud.",
  'graph.selectNodeForDetail': 'Sélectionnez un nœud pour voir le détail.',
  'graph.loadingDetail': 'Chargement du détail…',
  'graph.nodeRequiresTokenLead': 'Le détail de ce nœud nécessite un rôle',
  'graph.nodeRequiresTokenTail': '.',
  'graph.colorBy': 'Couleur par',
  'graph.colorByStatus': 'Statut',
  'graph.colorByKind': 'Type',
  'graph.colorByRole': 'Rôle',
  'graph.run': 'Run',
  'graph.latestRun': 'Dernier run',
  'graph.live': 'En direct',
  'graph.reload': 'Recharger',
  'graph.canvasHint': 'glisser les nœuds pour les organiser · molette pour zoomer',
  'graph.statusSuccess': 'succès',
  'graph.statusRunning': 'en cours',
  'graph.statusSkipped': 'ignoré',
  'graph.statusFailed': 'échoué',

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
    'Votre jeton actuel fonctionne toujours pour les autres onglets de Pollen — seule la recherche Mem0 nécessite un rôle supérieur.',
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
  // Ces libellés d'invite ne servent QUE de repli, quand le catalogue
  // (`/v1/tasks`, `/v1/projects`) est indisponible ou réellement vide. Le
  // chemin normal est une liste déroulante des valeurs connues du serveur.
  'runs.taskPlaceholder': 'Nom de la tâche',
  'runs.projectPlaceholder': 'Nom du projet',
  'runs.chooseTask': 'Choisir une tâche…',
  'runs.chooseProject': 'Choisir un projet…',
  'runs.taskCatalogueUnavailable':
    'Liste des tâches indisponible — saisissez le nom tel qu’il figure dans votre configuration.',
  'runs.projectCatalogueUnavailable':
    'Liste des projets indisponible — saisissez le nom tel qu’il figure dans votre configuration.',
  'runs.newRunTitle': 'Nouvelle exécution',
  'runs.newRunHelp':
    'Choisissez un projet et une tâche. L’exécution démarre aussitôt et apparaît sur le tableau.',
  'runs.newRunCloseAriaLabel': 'Fermer le formulaire de nouvelle exécution',
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

  // ---- Vue Run Board (section Opérations de Pollen — Kanban des exécutions) ----
  'board.description':
    'Statut en direct de chaque exécution, regroupé par étape — cliquez sur une carte pour le détail.',
  'board.descriptionReadOnly':
    "Statut en direct de chaque exécution, regroupé par étape (lecture seule — un jeton de rang run peut en déclencher de nouvelles).",
  'board.noRunsTitle': 'Aucune exécution pour le moment',
  'board.noRunsBody':
    'Chaque pipeline que vous déclenchez apparaît ici, regroupé par étape, et se rafraîchit tout seul toutes les quelques secondes. Lancez-en un pour remplir le tableau.',
  'board.noRunsBodyReadOnly':
    'Chaque pipeline déclenché sur ce tenant apparaît ici, regroupé par étape. Rien n’a encore tourné.',
  'board.noMatchTitle': 'Aucune exécution ne correspond à ces filtres',
  'board.noMatchBody':
    'Le tableau contient bien des exécutions, mais aucune pour ce couple projet / tâche.',
  'board.clearFilters': 'Réinitialiser les filtres',
  'board.allProjects': 'Tous les projets',
  'board.allTasks': 'Toutes les tâches',
  'board.density': 'Densité',
  'board.densityComfortable': 'Confortable',
  'board.densityCompact': 'Compacte',
  'board.showingCount': '{shown} exécutions sur {total}',
  'board.limit': 'Afficher',
  // Motifs d'échec / de pause, déduits du statut canonique de l'exécution —
  // le seul « pourquoi » réel exposé par la liste (`detail` est du texte
  // libre non fiable, jamais affiché).
  'board.reasonFailed': 'Le pipeline a signalé un échec.',
  'board.reasonDenied': 'Un approbateur a refusé cette exécution.',
  'board.reasonRateLimit': 'Interrompue par une limite de débit du fournisseur.',
  'board.reasonAuthExpired': 'Identifiants du fournisseur expirés.',
  'board.reasonTestFailure': 'Les tests ont échoué.',
  'board.reasonSecurityBlocker': 'Bloquée par un garde-fou de sécurité.',
  'board.reasonCancelled': 'Arrêtée par un opérateur.',
  'board.reasonPaused': 'Mise en pause en cours d’exécution — en attente de reprise.',
  'board.reasonDeferred': 'Reportée — nouvelle tentative plus tard.',
  'board.colQueued': 'File',
  'board.colRunning': 'En cours',
  'board.colWaitingApproval': "Attente d'approbation",
  'board.colFailed': 'Échec',
  'board.colDone': 'Terminé',
  'board.colOther': 'Autre',
  'board.cardAriaLabel': "Voir le détail de l'exécution {id} ({task} sur {project})",
  'board.listToggleLabel': 'Basculer en vue liste',
  'board.startedAgo': 'démarrée il y a {age}',
  'board.duration': 'a duré {duration}',
  'board.kanbanScrollLabel': 'Faire défiler les colonnes horizontalement',

  // ---- Panneau de détail d'exécution (section Opérations de Pollen) ----
  'runDetail.title': 'Exécution #{id}',
  'runDetail.closeAriaLabel': "Fermer le détail de l'exécution",
  'runDetail.stepsTitle': 'Étapes',
  'runDetail.noSteps': 'Aucun détail d’étape enregistré pour cette exécution.',
  'runDetail.overallDetail': 'Détail',
  'runDetail.provider': 'Fournisseur',
  'runDetail.model': 'Modèle',
  'runDetail.tokens': 'Tokens (entrée/sortie)',
  'runDetail.cost': 'Coût',
  'runDetail.loadFailed': "Échec du chargement du détail de l'exécution.",
  'runDetail.requiresTokenLead': "Le détail d'exécution nécessite un jeton de rang",
  'runDetail.requiresTokenTail': '(ou supérieur).',

  // ---- Memory quality view (tableau de bord de qualité de la mémoire) ---------
  'quality.kpiTitle': 'Qualité de la mémoire',
  'quality.searchSuccessRate': 'Taux de succès des recherches',
  'quality.noResultSearches': 'Recherches sans résultat',
  'quality.avgFreshness': 'Fraîcheur moyenne des rappels',
  'quality.declaredReliability': 'Fiabilité déclarée',
  'quality.onNSearches': 'sur {count} recherches',
  'quality.onNEvaluations': 'sur {count} évaluations',
  'quality.noSamples': 'Aucune donnée',
  'quality.noKpiData': 'Aucune recherche ni évaluation enregistrée sur cette période.',
  'quality.gapsTitle': 'Lacunes par namespace',
  'quality.acrossRuns': 'sur {count} runs',
  'quality.gapsDescription': 'Recherches sans résultat regroupées par namespace, les plus nombreuses en premier',
  'quality.noGaps': 'Aucune lacune de recherche enregistrée.',
  'quality.topQueriesLabel': 'requêtes principales :',
  'quality.evaluationsTitle': 'Évaluations récentes',
  'quality.evaluationsDescription': 'Retour humain « cette mémoire était-elle utile ? »',
  'quality.noEvaluations': "Aucune évaluation enregistrée pour l'instant.",
  'quality.useful': 'Utile',
  'quality.notUseful': 'Pas utile',
  'quality.journalTitle': 'Journal récent',
  'quality.journalDescription':
    'Événements mémoire les plus récents (recherche / lecture / stockage), les plus récents en premier',
  'quality.noJournal': "Aucune activité mémoire enregistrée pour l'instant.",
  'quality.colTs': 'Heure',
  'quality.colOp': 'Opération',
  'quality.colNamespace': 'Namespace',
  'quality.colQuery': 'Requête / clé',
  'quality.colResult': 'Résultat',
  'quality.colFreshness': 'Fraîcheur',
  'quality.colActor': 'Acteur',
  'quality.emptyTitle': 'Aucune activité mémoire enregistrée pour le moment',
  'quality.emptyState':
    "Ces indicateurs proviennent de l'instrumentation mem0, qui est facultative. Une fois activée, dès que les agents recherchent et stockent de la mémoire, le taux de succès des recherches, la fraîcheur du rappel et les manques par namespace apparaissent ici.",
  'quality.requiresTokenLead': 'Cette section nécessite un rôle',
  'quality.requiresTokenTail': 'à privilège supérieur.',
  'quality.requiresTokenNote':
    'Votre jeton actuel fonctionne toujours pour les autres onglets de Pollen — seule cette section nécessite un rôle supérieur.',

  // ---- Memory view (onglets unifiés Qualité/Croissance/Recherche) ------
  'memory.description':
    'Si la mémoire aide réellement (Qualité), quelle quantité elle représente (Croissance), et ce qu’elle contient (Recherche).',
  'memory.tabQuality': 'Qualité',
  'memory.tabGrowth': 'Croissance',
  'memory.tabSearch': 'Recherche',
  'memory.growthTitle': 'Croissance de la mémoire',
  'memory.growthDescription': 'Ce qui est stocké, où, dans le temps, et par qui.',
  'memory.totalMemories': 'Total des mémoires',
  'memory.byNamespaceTitle': 'Mémoires par namespace',
  'memory.growthOverTimeTitle': 'Croissance dans le temps',
  'memory.noGrowthSeries': 'Aucune croissance enregistrée sur cette période.',
  'memory.byActorTitle': 'Par acteur',
  'memory.authorshipNotAvailable':
    "Une répartition humain / agent n'est pas disponible — la vraie répartition par acteur est affichée à la place.",
  'memory.growthEmptyTitle': 'Rien de stocké sur cette période',
  'memory.growthEmptyState':
    'Les répartitions par namespace et par acteur se remplissent à mesure que les agents stockent des mémoires. Rien n’a été écrit ces 30 derniers jours.',

  // ---- Home view (vue d'accueil par défaut) -----------------------------
  'home.subtitle': "Votre flotte en un coup d'œil — cliquez sur un chiffre pour l'explorer.",
  'home.kpiSectionTitle': 'Aperçu',
  'home.refreshingLabel': 'Actualisation',
  'home.kpiSpendToday': 'Dépense du jour',
  'home.kpiSpendSub': 'dernières 24h',
  'home.kpiTokensSaved': 'Tokens économisés',
  'home.kpiTokensSavedSub': 'headroom + rtk, combinés',
  'home.kpiRunsSuccess': 'Runs · réussite',
  'home.kpiRunsSub': 'dernières 24h',
  'home.kpiMemoryHealth': 'Santé mémoire',
  'home.kpiMemorySub': 'taux de réussite des recherches',
  'home.kpiPendingApprovals': 'Approbations en attente',
  'home.kpiApprovalsSub': 'en attente de validation',
  'home.kpiRequiresRole': 'Nécessite un jeton de rang supérieur',
  'home.notAvailable': 'Non disponible',
  'home.noData': 'Aucune donnée',
  'home.needsAttentionTitle': 'À traiter',
  'home.needsAttentionDescription':
    "Approbations en attente les plus anciennes d'abord, puis les runs récemment échoués.",
  'home.allClear': "Tout est calme — rien ne nécessite votre attention pour l'instant.",
  'home.attentionApprovalBadge': 'Approbation',
  'home.attentionFailedRunBadge': 'Run échoué',
  'home.ageAgo': 'il y a {age}',
  'home.needsAttentionForbidden':
    'Les approbations et exécutions nécessitent un jeton de rang run (ou supérieur) pour être prévisualisées ici.',
  'home.sweepTitle': 'La flotte',
  'home.sweepDescription': 'Statut en direct des runs en cours et récents.',
  'home.sweepEmpty': 'Aucun run actif ou récent pour le moment.',
  'home.sweepLegendRunning': 'En cours',
  'home.sweepLegendWaiting': "En attente d'approbation",
  'home.sweepLegendFailed': 'Échoué',
  'home.sweepLegendIdle': 'Inactif / autre',
  'home.activityFeedTitle': "Flux d'activité",
  'home.activityFeedDescription': 'Runs et approbations les plus récents, les plus récents en premier.',
  'home.activityFeedEmpty': "Aucune activité pour l'instant.",
  'home.activityRunLabel': 'Run',
  'home.activityApprovalLabel': 'Approbation',

  // ---- Models view (section Dépenses de Pollen) ------------------------
  'models.title': 'Modèles',
  'models.tableScrollLabel': 'Tableau des modèles, faites défiler horizontalement pour voir les autres colonnes',
  'models.description': 'Coût, volume de tokens et taux de réussite par modèle',
  'models.noModels': "Aucune donnée de modèle pour l'instant.",
  'models.costPerSuccessfulRun': 'Coût par run réussi',
  'models.costPerSuccessfulRunSub': 'coût total / runs réussis',
  'models.noSucceededRuns': 'Aucun run réussi pour le moment',
  'models.shareOfSpendTitle': 'Part de la dépense',
  'models.shareOfSpendDescription': 'Répartition du coût entre les modèles',
  'models.successRate': 'Taux de réussite',
  'models.noAttempts': 'Aucune tentative',
  'models.latencyTitle': 'Latence',
  'models.latencyNotAvailable':
    "Non disponible — la latence p50/p95 ne peut pas être calculée à partir des données actuelles.",

  // ---- Efficiency view (section Dépenses de Pollen) ---------------------
  'efficiency.title': 'Efficacité',
  'efficiency.description': 'Signaux d’économie de tokens de la compression Headroom et de la CLI rtk',
  'efficiency.headroomTitle': 'Headroom',
  'efficiency.headroomDescription': 'Économies de compression de contexte enregistrées par le plugin Headroom',
  'efficiency.headroomNotAvailable': "Aucune compression enregistrée pour l'instant. Headroom ne réécrit que les contextes assez volumineux pour valoir une compression : tant qu'aucune exécution n'en produit, cette section reste vide — ce n'est pas un signe de panne (voir Santé pour cela).",
  'efficiency.headroomRanAndDeclined':
    "Headroom s'est exécuté et a renoncé {count} fois — aucun contexte n'était assez volumineux pour valoir une réécriture. Il fonctionne, il n'y a simplement rien à comprimer.",
  'efficiency.headroomNeverRan':
    "Headroom ne s'est jamais exécuté — ni compression ni saut enregistré. Voir Santé si tu t'attendais à ce qu'il tourne.",
  'efficiency.tokensSaved': 'Tokens économisés',
  'efficiency.compressions': 'Compressions enregistrées',
  'efficiency.charsSaved': 'Caractères économisés',
  'efficiency.avgCompressionRate': 'Taux de compression moyen',
  'efficiency.p95Ratio': 'Ratio P95 (pire cas)',
  'efficiency.p95RatioSub': 'de la taille originale conservée',
  'efficiency.cacheTitle': 'cache de prompt',
  'efficiency.cacheDescription': "Mesuré depuis notre propre télémétrie par étape, pas depuis le rapport d'un outil. Le taux global n'est pas le sujet — la liste ci-dessous l'est.",
  'efficiency.cacheNotMeasured': "Aucune étape modèle n'a encore tourné — rien à mesurer.",
  'efficiency.cacheHitRate': 'taux de cache',
  'efficiency.cacheSteps': '{count} étapes modèle',
  'efficiency.cacheRead': 'cache relu',
  'efficiency.cacheCreated': 'cache créé',
  'efficiency.cacheUnamortisedTitle': "Étapes qui créent du cache qu'elles ne relisent jamais",
  'efficiency.cacheUnamortisedDescription': "Médiane par run sous 1,0 : l'étape a créé plus de cache qu'elle n'en a jamais réutilisé. Une création coûte 1,25x l'entrée, une lecture 0,1x — donc plein tarif, à chaque run. Généralement du variable placé avant du stable dans le prompt.",
  'efficiency.cacheColStep': 'étape',
  'efficiency.cacheColRuns': 'runs',
  'efficiency.cacheColCreated': 'créé',
  'efficiency.cacheColRead': 'relu',
  'efficiency.cacheColAmortisation': 'amortissement',
  'efficiency.proxyTitle': 'proxy de compression',
  'efficiency.proxyDescription':
    "Sur le chemin de chaque appel d'agent. Il bascule en direct quand il est injoignable, ce qui laisse les agents fonctionner tout en cessant silencieusement de les compresser — c'est ici que ça se voit.",
  'efficiency.proxyNotAvailable': 'ne répond pas — non configuré, ou arrêté. Les agents partent en direct.',
  'efficiency.proxyRequests': 'Requêtes',
  'efficiency.proxyMode': 'mode {mode}',
  'efficiency.proxyCompressed': 'Compressées',
  'efficiency.proxyTokensRemoved': 'Tokens retirés',
  'efficiency.proxySaved': 'Économisé',
  'efficiency.proxyAgainst': 'sur {total}',
  'efficiency.rtkTitle': 'rtk',
  'efficiency.rtkDescription': 'Économies de tokens globales par commande (CLI rtk), non limitées au tenant',
  'efficiency.rtkNotAvailable': "rtk n'est pas disponible sur cet hôte",
  'efficiency.rtkGain': 'Gain rtk',
  'efficiency.rtkTokensSaved': 'Tokens économisés (rtk)',
  'efficiency.rtkCommands': 'Commandes suivies',
  'efficiency.rtkSavedSeriesTitle': 'Tendance des économies',
  'efficiency.noSavedSeries': "Aucune série journalière enregistrée pour le moment.",

  // ---- Autopilot view ---------------------------------------------------
  'autopilot.description':
    "La file d'objectifs surveillée — mettez en pause, reprenez, et surveillez ce qu'elle déclenche.",
  'autopilot.statusLabel': 'Statut',
  'autopilot.active': 'Actif',
  'autopilot.paused': 'En pause',
  'autopilot.queueDepthLabel': 'Profondeur de la file',
  'autopilot.pauseButton': 'Mettre en pause',
  'autopilot.resumeButton': 'Reprendre',
  'autopilot.pauseConfirm':
    "Mettre l'autopilote en pause ? Il arrêtera de déclencher de nouveaux objectifs jusqu'à la reprise.",
  'autopilot.resumeConfirm':
    "Reprendre l'autopilote ? Il recommencera à déclencher les objectifs en file.",
  'autopilot.insufficientRole':
    "Rôle insuffisant — votre jeton ne peut plus mettre en pause/reprendre l'autopilote.",
  'autopilot.controlRequiresRunRole':
    'Nécessite un jeton de rang run (ou supérieur) pour mettre en pause/reprendre.',
  'autopilot.forbidden': "Impossible de charger l'état de l'autopilote pour le tenant de votre jeton.",
  'autopilot.budgetTitle': 'Budget',
  'autopilot.dailyBudget': 'Budget quotidien',
  'autopilot.spentToday': "Dépensé aujourd'hui",
  'autopilot.remaining': 'Restant',
  'autopilot.blockedByBudget': 'Bloqué — budget consommé',
  'autopilot.spentTodayTenant': "Dépensé aujourd'hui (tout confondu)",
  'autopilot.remainingTenant': 'Budget restant aujourd’hui',
  'autopilot.unknown': 'inconnu',
  'autopilot.noBudgetTitle': 'Aucun plafond de dépense quotidien',
  'autopilot.noBudgetBody':
    'Renseignez budget_daily_usd dans policies.yaml pour plafonner ce que l’autopilote peut dépenser par jour. Sans ce plafond, les dispatches ne sont soumis à aucun garde-fou budgétaire.',
  'autopilot.budgetBurn': 'Consommation du budget',
  'autopilot.queueTitle': "File d'objectifs",
  'autopilot.queueEmptyTitle': 'Aucun objectif en file',
  'autopilot.queueEmptyBody':
    'Les objectifs arrivent ici lorsqu’un pipeline planifié ou une analyse de dérive en soulève un. L’autopilote en traite au plus un par cycle.',
  'autopilot.enqueuedAgo': 'ajoutée il y a {age}',
  'autopilot.dispatchesTitle': 'Dispatches récents',
  'autopilot.dispatchesEmptyTitle': 'Aucun dispatch pour le moment',
  'autopilot.dispatchesEmptyBody':
    'Un dispatch est enregistré chaque fois que l’autopilote sort un objectif de la file — uniquement pour les pipelines autorisés ci-dessous.',
  'autopilot.allowlistTitle': 'Pipelines autorisés',
  'autopilot.allowlistEmptyTitle': 'Aucun pipeline ne peut être lancé automatiquement',
  'autopilot.allowlistEmptyBody':
    'L’autopilote peut toujours mettre des objectifs en file, mais il n’en exécutera aucun. Ajoutez un pipeline à auto_dispatch dans policies.yaml pour l’autoriser à agir.',

  // ---- Partitions view (propose -> ratify -> dispatch PRD, Sprint 4) ----
  // Registre sobre et littéral, comme en anglais : rien ici n'enjolive ce qui
  // revient, au fond, à « vous allez lancer N agents et pousser du code vers
  // l'extérieur ».
  'partitions.description':
    "Une partition découpe un même travail en tâches budgétées. Relisez le plan, modifiez-le, puis lancez. Rien ne démarre tant que vous n'avez pas ratifié.",
  'partitions.descriptionReadOnly':
    "Une partition est un plan découpé en tâches, proposé par un agent, qu'un humain ratifie avant que quoi que ce soit ne démarre. Lecture seule ici — ratifier demande un jeton de rang approve.",
  'partitions.forbidden':
    'Cette liste demande un jeton de rang run (ou supérieur). Votre jeton reste valable sur tous les autres onglets de Pollen.',
  'partitions.emptyTitle': 'Aucune partition',
  'partitions.emptyBody':
    "Rien n'en a proposé. Une partition arrive quand un pipeline soumet un plan "
    + '(hivepilot partition submit --file plan.json). Aucun de vos pipelines ne le fait '
    + "encore — quel proposeur ajouter, et ce qu'une tâche signifie pour vous, relève de votre config, pas du moteur.",
  'partitions.review': 'Relire',
  'partitions.reviewAriaLabel': 'Relire la partition {id}',
  'partitions.sourceLabel': 'Source',
  'partitions.proposedAgo': 'proposée il y a {age}',
  'partitions.notRatifiable':
    'Seule une partition proposée peut être ratifiée. Celle-ci est {status}.',

  // ---- panneau de ratification ----------------------------------------
  'partitions.drawerTitle': 'Ratifier la partition',
  'partitions.drawerAriaLabel': 'Ratifier la partition {id}',
  // Un seul panneau porte la ratification et le journal : le bouton de
  // fermeture est donc nommé d'après le panneau, pas d'après l'un des deux.
  'partitions.closeAriaLabel': 'Fermer le panneau de partition',
  'partitions.planTitle': 'Plan',
  'partitions.planLabel': 'Plan de partition (JSON)',
  'partitions.planHint':
    "C'est exactement ce qui va s'exécuter. Modifiez-le directement, ou passez par les contrôles de tâches ci-dessous — les deux écrivent dans le même document.",
  'partitions.parseErrorLead': 'Ce JSON est invalide, donc rien ne peut être lancé :',
  'partitions.checking': 'Vérification du plan…',
  'partitions.gateAccepted': 'Le garde-fou accepte ce plan.',
  'partitions.gateRefusedLead': 'Le garde-fou refuserait ce plan :',
  'partitions.previewUnavailable':
    "Le plan n'a pas pu être confronté à la politique en vigueur : le lancement reste désactivé.",

  // ---- contrôles typés -------------------------------------------------
  'partitions.tasksTitle': 'Tâches',
  'partitions.noTasks': 'Ce plan ne déclare aucune tâche',
  'partitions.noTasksBody':
    'Une partition sans tâche ne lance rien. Ajoutez une tâche dans le JSON ci-dessus, ou rechargez la proposition.',
  'partitions.dropTask': 'Retirer',
  'partitions.dropTaskAriaLabel': 'Retirer la tâche {id} de ce plan',
  'partitions.dependsOn': 'après {ids}',
  'partitions.wallClockLabel': 'Arrêt forcé au bout de',
  'partitions.wallClockAriaLabel':
    "Plafond de temps d'exécution en secondes pour la tâche {id}",
  'partitions.wallClockHelp':
    "Un plafond appliqué, pas une estimation : la tâche est arrêtée d'office à ce moment-là.",
  'partitions.costLabel': 'Plafond de coût ($)',
  'partitions.costAriaLabel': 'Plafond de coût en dollars pour la tâche {id}',
  'partitions.costHelp':
    "Contrôle d'admission : la somme est confrontée au budget quotidien restant. C'est une vérification préalable, pas une réservation — une vague peut la dépasser.",
  'partitions.planCostLabel': 'Plafond de coût du plan',
  'partitions.wavesLabel': 'Vagues',
  'partitions.waveNumber': 'Vague {index}',

  // ---- consentement vers l'extérieur -----------------------------------
  'partitions.outwardTitle': "Action visible à l'extérieur",
  'partitions.outwardWarning': '{actions} — une action visible en dehors de cette machine.',
  'partitions.outwardConsentLabel': "Je consens à ces actions visibles à l'extérieur",
  'partitions.outwardNoneTitle': "Rien vers l'extérieur",
  'partitions.outwardNoneBody':
    "Avec la configuration en vigueur, ce plan ne pousse rien et n'ouvre rien. Le travail reste sur cette machine : aucun consentement n'est requis.",
  'partitions.outwardV1Gap':
    "Le consentement n'est appliqué au lancement que pour les actions git et forge. notify, vault_write et external_api sont nommés ci-dessus mais ne sont pas encore bloqués à l'exécution.",
  'partitions.outwardHonesty':
    "Ceci régit ce que fait le moteur. Un agent ayant accès au shell peut toujours agir vers l'extérieur de son côté.",
  'partitions.outwardAction.git_push': 'des branches seront poussées',
  'partitions.outwardAction.forge_pr': 'des PR ouvertes',
  'partitions.outwardAction.forge_merge': 'des PR fusionnées',
  'partitions.outwardAction.forge_issue': 'des tickets ouverts',
  'partitions.outwardAction.forge_release': 'des releases publiées',
  'partitions.outwardAction.notify': 'des notifications envoyées',
  'partitions.outwardAction.vault_write': 'des notes écrites dans votre vault',
  'partitions.outwardAction.external_api': 'des API externes appelées',

  // ---- parallélisme effectif -------------------------------------------
  'partitions.parallelismLabel': 'Parallélisme effectif',
  'partitions.parallelismSub': '{requested} demandés',
  'partitions.parallelismTitle': 'Ce qui tourne réellement en parallèle',

  // ---- lancement -------------------------------------------------------
  'partitions.dispatch': 'Ratifier et lancer',
  'partitions.dispatchAriaLabel': 'Ratifier et lancer la partition {id}',
  'partitions.dispatchConfirm':
    'Lancer {count} tâche(s) maintenant ? Cela démarre de vrais agents, au plus {effective} à la fois. Une partition ratifiée ne peut plus être modifiée.',
  'partitions.dispatchBlocked':
    "Le lancement reste désactivé tant que le plan n'est pas valide et accepté par le garde-fou.",
  'partitions.dispatched':
    'Ratifiée. {count} tâche(s) en file — le lancement se poursuit en arrière-plan.',
  'partitions.idempotent': "Déjà ratifiée. Rien n'a été lancé une seconde fois.",
  'partitions.warningsTitle': 'Avertissements',
  'partitions.insufficientRole':
    'Votre jeton ne permet pas de ratifier une partition : il faut un jeton de rang approve.',
  'partitions.errorStale':
    "Cette partition a changé depuis que vous l'avez ouverte. Rechargez-la et relisez le nouveau plan avant de ratifier.",

  // ---- journal de lancement (sprint 5) ---------------------------------
  // Le journal est un compte rendu, pas un tableau de bord : il dit ce qui
  // s'est passé, qui l'a déclenché, ce que ça a coûté et ce qui en est
  // sorti. Là où le moteur ne sait pas, il le dit — un tiret cadratin pour
  // « non enregistré », le mot « inconnu » pour « enregistré mais non
  // mesurable ». Jamais un zéro, jamais une supposition.
  'partitions.history': 'Journal',
  'partitions.historyAriaLabel': 'Ouvrir le journal de lancement de la partition {id}',
  'partitions.journalTitle': 'Journal de lancement',
  'partitions.journalDrawerTitle': 'Journal de la partition',
  'partitions.journalDrawerAriaLabel': 'Journal de lancement de la partition {id}',
  'partitions.journalScrollLabel':
    'Journal de lancement — faites défiler horizontalement pour voir les autres colonnes',
  'partitions.journalEmptyTitle': 'Aucun lancement pour le moment',
  'partitions.journalEmptyBody':
    "Une ligne apparaît ici par tâche dès que cette partition est ratifiée et que sa première vague est réservée. Une partition seulement proposée n'a encore rien à raconter.",
  'partitions.colTask': 'Tâche',
  'partitions.colStatus': 'Statut',
  'partitions.colActor': 'Auteur',
  'partitions.colClaimed': 'Réservée le',
  'partitions.colPr': 'PR',
  'partitions.colCost': 'Coût',
  'partitions.colAttempt': 'Tentative',
  'partitions.costUnknown': 'inconnu',
  'partitions.costUnknownTitle':
    "Cette tâche est arrivée à son terme mais aucune étape n'a rapporté de coût. La dépense est inconnue, pas nulle.",
  'partitions.prNoneTitle': "Aucune URL de pull request n'a été enregistrée pour cette tâche.",
  'partitions.prNotWebTitle':
    "Reproduit tel quel : cette valeur n'est pas une URL http(s), elle est donc affichée comme du texte et non transformée en lien.",
  'partitions.prAriaLabel': 'Ouvrir la pull request enregistrée pour la tâche {id}',
  'partitions.journalPrNote':
    "Une tâche sans lien de PR affiche —. Le moteur attribue chaque pull request à la tâche qui l'a réellement ouverte, via l'identité d'exécution propre à cette tâche — les tâches lancées en même temps sur le même projet obtiennent donc chacune leur propre lien, et une PR ouverte par une exécution qui lui est étrangère n'est jamais revendiquée. — reste réservé aux cas où il n'existe vraiment pas de réponse unique : la forge n'a signalé aucune URL, le consentement à l'action visible à l'extérieur a été refusé, ou l'exécution d'une tâche a ouvert plusieurs PR. Un lien manquant est un trou ; un mauvais lien serait un mensonge.",
  'partitions.journalSkippedNote':
    "skipped signifie que la tâche n'a jamais démarré parce qu'un prérequis a échoué — enregistrée délibérément comme skipped et non comme failed.",

  // ---- Agents view (Mirador Agent Panels backend sprint frontend) ------
  'agents.title': 'Agents',
  'agents.description':
    'Ce que coûte chaque rôle et à quelle fréquence il réussit. Sélectionnez un rôle pour ses leçons et ses verdicts.',
  'agents.colRole': 'Rôle',
  'agents.tableScrollLabel': 'Tableau des agents, faites défiler horizontalement pour voir les autres colonnes',
  'agents.rowAriaLabel': 'Ouvrir le détail de {name}',
  'agents.attentionTitle': '{count} rôle(s) à surveiller',
  'agents.allClear': 'Tous les rôles sont nominaux — aucun verdict négatif, aucun taux de réussite faible.',
  'agents.reasonVerdict': 'dernier verdict autre qu’une acceptation',
  'agents.reasonLowSuccess': 'taux de réussite de {rate} %',
  'agents.noRoster': 'Aucun rôle configuré',
  'agents.noRosterBody':
    'Les rôles se déclarent dans votre configuration (roles.yaml). Dès qu’un rôle existe et qu’une exécution lui attribue une étape, son coût et son taux de réussite apparaissent ici.',
  'agents.forbidden': "Impossible de charger l'activité des agents pour le tenant de votre jeton.",
  'agents.verdictsForbidden':
    "Impossible de charger les signaux de sévérité des verdicts pour le tenant de votre jeton.",
  'agents.noActivityYet': 'Aucune activité attribuée pour le moment.',
  'agents.noAttemptsYet': 'Aucune tentative',
  'agents.attentionBadge': 'À surveiller',
  'agents.costLabel': 'Coût',
  'agents.runsLabel': 'Exécutions',
  'agents.stepsLabel': 'Étapes',
  'agents.tokensLabel': 'Jetons (entrée/sortie)',
  'agents.lastActiveLabel': 'Dernière activité',
  'agents.successRateLabel': 'Taux de réussite',
  // L'ancienne formulation présentait ce bucket comme de l'historique
  // antérieur à l'attribution par rôle. Sur les données réelles, c'était
  // faux pour chacune de ses lignes : l'essentiel est constitué d'étapes
  // shell qui ne peuvent pas avoir de rôle, et le reliquat est du vrai
  // travail modèle non attribué, porteur de dépense. Les trois causes sont
  // désormais nommées séparément, car une seule est un défaut.
  'agents.unknownTitle': 'Étapes sans agent',
  'agents.unknownDescription':
    "Exclues de tous les chiffres ci-dessus. Deux de ces causes sont structurelles ; la troisième est un manque à combler.",
  'agents.unknown.noModel': 'Aucun agent impliqué',
  'agents.unknown.noModelHint': "Étapes shell — rien n'a tourné qui puisse avoir un rôle.",
  'agents.unknown.skipped': 'Ignorées',
  'agents.unknown.skippedHint': "L'étape n'a jamais tourné, elle n'a donc rien appelé.",
  'agents.unknown.attributionGap': 'Exécutées sans rôle enregistré',
  'agents.unknown.attributionGapHint':
    "Un modèle a tourné sans qu'aucun rôle soit enregistré. C'est la seule cause ici qui soit un défaut.",
  'agents.unknown.gapCost': '{cost} de dépense manquent aux chiffres par agent ci-dessus.',
  'agents.unknown.stepsSuffix': '{count} étapes',
  'agents.detailAriaLabel': "Détail de l'agent : {name}",
  'agents.closeAriaLabel': "Fermer le détail de l'agent",
  'agents.lessonsTitle': 'Leçons récentes',
  'agents.noLessons': 'Aucune leçon enregistrée pour ce rôle pour le moment.',
  'agents.verdictsTitle': 'Verdicts récents',
  'agents.noVerdicts': 'Aucun verdict enregistré pour ce rôle pour le moment.',
  'agents.validated': 'Validée',
  'agents.candidate': 'À valider',
  'agents.scoreLabel': 'score {score}',
  'agents.confidenceLabel': '{confidence}% de confiance',
  'agents.unknownKind': 'inconnu',
  'agents.noDecision': 'Aucune décision confiante',
}
