# Plan — Système de plugins

**Statut :** nouveau PRD (à formaliser via `/plan`). À construire **APRÈS** la CLI d'édition de conf.
Base existante : `hivepilot/plugins.py` (hooks `before_step`/`after_step`, `load_plugins()` scan `plugins/*.py` + `register()`, câblé `orchestrator.py:1763,1866`). **On étend, on ne réécrit pas.**

## Décisions figées
- **Portée v1 :** runners **+** notifiers **+** hooks step (les trois).
- **Mécanismes :** scan local (`plugins/*.py` / config-repo) **ET** entry-points pip (`[project.entry-points]`) **dès le départ**.

## Constat / contrainte centrale
- Blocage runners : `RunnerKind = Literal[...]` fermé (`models.py:8`) + `RUNNER_MAP` dict statique importé en dur (`registry.py:8-34`). Pydantic rejette tout `kind` hors des 12 valeurs.
- Définir une nouvelle *instance* de runner (même kind) marche déjà via `tasks.yaml` — le plugin ne sert qu'à un *nouveau kind/comportement*.
- Notifiers : `notification_service.py:52-66` = if/elif statique slack/discord/telegram (même patron à ouvrir).

## Sprint 1 — Ouvrir le registre de runners (fondation)
- [ ] `RunnerKind` : `Literal[...]` → `str` ; garder `KNOWN_RUNNER_KINDS: tuple` (built-ins, pour aide/typing).
- [ ] Remplacer `RUNNER_MAP` statique par `RunnerRegistry.register(kind: str, cls: type[BaseRunner])` ; les 12 built-ins s'auto-enregistrent au démarrage.
- [ ] Validation runtime contre le **registre vivant** au lieu de `get_args(RunnerKind)` (corriger `orchestrator.py:152`).
- [ ] Vérifier que les ~15 `cast(RunnerKind, ...)` restent OK (casts statiques, pas d'effet runtime).
- [ ] Tests : enregistrer un kind factice, l'orchestrateur le résout et l'exécute.

## Sprint 2 — Chargement des plugins (les deux mécanismes)
- [ ] Étendre le contrat `register()` de `plugins.py` : retourne `{"runners": {kind: cls}, "notifiers": {name: fn}, "before_step": ..., "after_step": ...}`.
- [ ] Injecter `runners` dans `RunnerRegistry.register()` au `PluginManager.__init__`.
- [ ] Ajouter `[project.entry-points."hivepilot.plugins"]` (ou `.runners`/`.notifiers`) + `importlib.metadata.entry_points(group=...)`.
- [ ] Précédence claire scan-local vs entry-points ; collisions de kind → erreur explicite.
- [ ] Tests : plugin via fichier local + plugin via entry-point (fixture package), les deux découverts.

## Sprint 3 — Notifiers pluggables
- [ ] Transformer `notification_service.send_notification` if/elif → registre `NotifierRegistry.register(name, fn)`.
- [ ] slack/discord/telegram built-in s'auto-enregistrent ; plugin peut ajouter un canal.
- [ ] Tests : notifier plugin custom reçoit l'événement.

## Sprint 4 — Hooks étoffés + doc + sécurité
- [ ] Élargir les hooks : `on_pipeline_start`, `on_pipeline_end`, `on_error` (en plus de before/after_step).
- [ ] **Modèle de confiance documenté** : plugins chargés seulement depuis config-repo de confiance et/ou packages installés ; pas de fetch distant auto ; note claire "un plugin = code arbitraire".
- [ ] `hivepilot plugins list` (inventaire des plugins chargés + provenance).
- [ ] Doc `docs/v4/PLUGINS.md` : écrire un runner/notifier/hook, packaging pip, exemple.

## Risques
- 🔐 Exécution de code arbitraire → modèle de confiance explicite (Sprint 4).
- 🧩 Ouvrir le `Literal` affaiblit le typage statique → mitigé par `KNOWN_RUNNER_KINDS` + validation runtime.
- Régression : le check `get_args(RunnerKind)` est le seul point runtime sensible — couvrir par test.

## Vérif de complétude
- [ ] Un runner tiers (nouveau kind) chargé via fichier local ET via package pip s'exécute dans un pipeline réel.
- [ ] Un notifier tiers reçoit un événement.
- [ ] Les 12 runners + 3 notifiers built-in fonctionnent toujours (non-régression).
- [ ] `plugins list` montre provenance ; doc PLUGINS.md complète.

## Exécution
`/plan` pour générer le PRD + specs sprints (max 5) → build-candidate → `/plan-build-test`.
