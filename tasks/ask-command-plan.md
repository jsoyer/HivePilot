# Plan — Ordres directs à un agent via Telegram (`/ask` + alias)

## Objectif
Donner un ordre direct à UN agent sans dérouler le cycle CEO→…→Docs.
Un seul bot (pas de bot-par-rôle). `/ask` opère au **niveau projet/groupe**
(là où `/run` est spécifique à un composant + nom de tâche).

## Comportement
- `/ask <agent> [@cible] <ordre>` → lance la tâche `company-<role>` de l'agent,
  avec `<ordre>` injecté en `extra_prompt`, **auto_git = True** (commit/push/PR
  comme le pipeline — choix utilisateur).
- `[@cible]` optionnelle = projet ou groupe. Absente → cible par défaut
  (`HIVEPILOT_DEFAULT_TARGET`, défaut `noxys`). Si la cible est un **groupe**,
  l'ordre tourne sur le **hub** (les rôles planif s'y prêtent ; pour un ordre de
  code, préciser `@<composant>`, ex. `@noxys-api`).
- Résolution `<agent>` tolérante : alias court, nom de rôle, ou nom d'agent
  (`dev` = `developer` = `gustave` → role developer).

## Alias implémentés (1 commande par agent)
| Commande | Agent | Rôle | Tâche |
|---|---|---|---|
| `/ceo`    | Aliénor | ceo            | company-ceo-intake |
| `/jules` (alias `/cos`) | Jules | chief_of_staff | company-cos-synthesis |
| `/cto`    | Blaise  | cto            | company-cto-review |
| `/dev`    | Gustave | developer      | company-developer |
| `/review` (alias `/reviewer`) | Victor | reviewer | company-reviewer |
| `/ciso`   | Hugo    | ciso           | company-ciso |
| `/qa`     | Marie   | qa             | company-qa |
| `/docs`   | Théo    | documentation  | company-documentation |
| `/henri`  | Henri   | (auditeur)     | auditor_service (hors ROLES) |
| `/ask`    | générique | <agent> en 1er arg | route vers la tâche ci-dessus |

## Tâches d'implémentation
- [ ] `telegram_bot.py` : `AGENT_COMMANDS` (alias→(role, task, display)) +
      `_resolve_agent(token)` (alias/role/display-name, insensible à la casse).
- [ ] `_cmd_ask` : parse `<agent> [@cible] <ordre>`, résout cible (défaut config),
      appelle `orchestrator.run_task(project, task, extra_prompt=ordre, auto_git=True)`.
- [ ] Handlers fins par alias (`/dev`, `/cto`, …) déléguant à `_cmd_ask` avec
      l'agent pré-fixé ; `/henri` route vers `auditor_service`.
- [ ] Enregistrer tous les handlers + mettre à jour `/help`.
- [ ] `config.py` : `default_target: str = "noxys"` (`HIVEPILOT_DEFAULT_TARGET`).
- [ ] Tests : résolution d'alias, parsing `@cible` + défaut, alias de noms d'agent.
- [ ] Doc : section "Ordres directs" dans `docs/v4/USAGE.md`.

## Hors scope (noté, pas fait)
- Bot par rôle (rejeté : N tokens/services pour un gain cosmétique).
- Variante sans-git (l'utilisateur a choisi git-auto ; flag possible plus tard).

## Sécurité
- `/dev` + auto_git + `bypassPermissions` = écrit/commit/PR réellement. Sur cible
  groupe, tourne sur le hub ; pour du code préférer `@<composant>`. Mêmes
  garde-fous que le pipeline (PR ouverte, humain merge — `merge_pr: false`).
