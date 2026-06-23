# Plan — Résilience au quota claude (dev fan-out)

Objectif : que le dev (Gustave/claude) sur N composants **tienne sur un quota limité**
et **reprenne tout seul**. 4 briques. Primitive partagée = détection d'erreur quota.

## Primitive — détection d'erreur quota (partagée par 2 & 3)
`quota.py::parse_quota_error(stderr) -> QuotaError | None`
- Reconnaît `You've hit your session limit · resets <HH:MMam/pm> (<TZ>)`.
- Extrait l'heure de reset → `datetime` (prochaine occurrence).
- Classe l'échec en **QUOTA** (différable), distinct d'un échec de code.
- Le claude_runner lève déjà `RuntimeError("claude exited 1: …")` → on parse ce message.

## Brique 1 — Throttle claude (concurrency 1-2)
- Config `claude_max_concurrency: int = 1`.
- Sémaphore **par runner kind** (threading.Semaphore) acquis dans `_execute_task`
  autour de l'appel claude. Le fan-out (ThreadPoolExecutor) peut garder conc=4 pour
  les autres runners, mais claude est plafonné à 1-2.
- Empêche de brûler le quota d'un coup.

## Brique 2 — Fallback de provider pour le dev
- Config `dev_fallback_runners: list[str] = ["codex", "opencode"]` (ou champ rôle
  `fallback_runners`). Le prompt de Gustave liste déjà ces fallbacks.
- Dans `_execute_task` (chemin rôle) : si l'appel lève une **QuotaError** (ou échec),
  rejouer le MÊME step avec le runner de fallback suivant (même prompt/contexte).
- → le dev continue **sans attendre le reset**. Plus gros levier.
- Garde-fou : ne fallback que pour les erreurs quota/transitoires, pas les échecs de code.

## Brique 3 — Auto-resume quota-aware (filet de sécurité)
- Sur QuotaError et si fallback désactivé/épuisé : **enfiler dans `retry_queue`**
  (table + `retry_service` déjà présents) avec `next_attempt_at = reset_time + jitter`,
  status « deferred (quota) ».
- Stocker dans l'entrée le contexte nécessaire (project, task, extra_prompt, plan).
- Le **scheduler daemon** (`hivepilot schedule daemon`, déjà là) ré-exécute les
  composants dus **automatiquement** à la réouverture de la fenêtre → reprise sans
  re-trigger manuel.
- **Idempotent** : ne relance que les composants non réussis (la L3 stage-cache aide).

## Brique 4 — Batching par fenêtre
- Config `dev_batch_size: int` : ne tenter que B composants par passe, différer le reste
  (via brique 3). Émergent en partie de throttle(1)+auto-resume, mais rendu explicite
  pour borner la conso par fenêtre.

## Ordre de build (séquentiel, isolé, APRÈS libération du quota par le dev-test)
1. Primitive `parse_quota_error` + tests.
2. Brique 1 (throttle) — petit, sûr.
3. Brique 2 (fallback dev) — s'appuie sur la primitive.
4. Brique 3 (auto-resume via retry_queue + daemon).
5. Brique 4 (batching).

## Note quota meta
Mes sous-agents (claude) ET le dev (claude CLI) partagent le quota du compte → ne pas
lancer de build claude pendant un dev claude. Construire quand le dev-test est fini.
