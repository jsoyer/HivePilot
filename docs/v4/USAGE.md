# HivePilot V4 — Usage (CLI & Telegram)

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e .          # lightweight core
.venv/bin/pip install -e ".[notifications]"                 # + Telegram bot
.venv/bin/pip install -e ".[langchain]"                     # + RAG (langchain+torch, optional)
.venv/bin/pip install -e ".[dashboard]"                     # + Textual dashboard
```

`hivepilot doctor` checks paths, external binaries, and the **agent runner CLIs**
(claude / codex / gemini / opencode / cursor) on PATH.

## CLI reference

| Command | What it does |
|---|---|
| `hivepilot run <project> <task> [-e "extra"] [--auto-git]` | Run a single task |
| `hivepilot run-pipeline <project> <pipeline> [--simulate] [--auto-git]` | Run a pipeline (e.g. `company`) |
| `hivepilot debate <project> <topic> [--role ceo] [--simulate]` | CEO dual-model debate → ADR |
| `hivepilot run-pipeline … --simulate` | Preview wiring: records steps, **no real agent calls**, bypasses approval |
| `hivepilot approvals` / `… run-approved` | List / act on pending approvals |
| `hivepilot list-pipelines` / `list-projects` / `list-tasks` | Discovery |
| `hivepilot tokens add --role admin` | Mint an API/CLI token (first must be admin) |
| `hivepilot dashboard` | Textual TUI: runs, steps, interactions (needs `[dashboard]`) |
| `hivepilot telegram` | Start the Telegram bot (polling; needs `[notifications]` + token) |
| `hivepilot doctor` | Environment / readiness check |

**Dry-run vs simulate:** `--dry-run` (default true) only skips *vault writes*;
the agents still run. `--simulate` skips *agent execution* entirely (safe preview).

### Typical Noxys run

```bash
hivepilot run-pipeline noxys company --simulate          # validate wiring (no calls, no approval)
hivepilot run-pipeline noxys company --auto-git          # real run -> queued for approval
hivepilot approvals                                      # see the pending run
# approve via CLI or Telegram, then agents execute; developer opens a PR you merge
```

## Telegram — remote command & control

Enable: `pip install -e ".[notifications]"`, then set
`HIVEPILOT_TELEGRAM_BOT_TOKEN` (from @BotFather) and
`HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated whitelist; empty = open to all),
then `hivepilot telegram`.

| Command | |
|---|---|
| `/run <project> <task> [instructions]` | run a task |
| `/runpipeline <project> <pipeline> [simulate]` | run a pipeline |
| `/debate <project> <topic>` | CEO debate → ADR |
| `/status` | last runs |
| `/interactions [limit]` | what the agents are doing |
| `/steps <run_id>` | detail of one run's steps |
| `/approvals`, `/approve <id>`, `/deny <id> [reason]` | approve/deny runs (control) |
| `/pipelines`, `/projects`, `/tasks` | discovery |
| `/diff <project>`, `/rollback <project>` | git inspect / revert |
| `/help` | command list |

This gives full remote control: launch the company, watch interactions/steps,
and gate execution via approvals — from your phone.

### Live agent streaming (Telegram)

During a pipeline run (and CEO debate), HivePilot live-streams each agent's
turn to Telegram via `sendMessage` as it happens, so you watch the agents
hand off to each other in real time. Each message shows an icon + the agent's
display name (FR theme: Aliénor/Jules/Blaise/Gustave/Victor/Hugo/Marie/Théo)
+ the stage name, the next agent it hands off to (`↳`), and a short summary.

| Icon | Meaning |
|---|---|
| 🚀 | pipeline start |
| 🗣 | pipeline hand-off (an agent's turn) |
| 💬 | debate model proposal |
| ⚖️ | debate synthesis |

This requires `HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID` configured (plus a bot
token); it is Telegram-only and a silent no-op if Telegram is not configured.
On by default — turn it off with `HIVEPILOT_TELEGRAM_STREAM_LIVE=false`.

See [ARCHITECTURE.md](ARCHITECTURE.md), [AGENTS.md](AGENTS.md), [CONFIG.md](CONFIG.md), [NOXYS.md](NOXYS.md).


## Plan checkpoint (validation du plan avant le dev)

Une étape de pipeline marquée `pause_before: true` met le pipeline **en pause
juste avant de l'exécuter**, le temps que tu valides le plan produit par les
étapes précédentes.

Dans le pipeline `company`, l'étape **Implementation** (le développeur, Gustave)
porte ce flag : le pipeline déroule donc CEO → Plan (Jules) → Spec CTO (Blaise),
écrit le plan dans Obsidian, puis **s'arrête** et t'envoie dans Telegram un message
avec boutons **✅ Approve / ❌ Deny** (et le live `⏸️ checkpoint`).

- **Approuver** : `/approve <run_id>` (ou le bouton) → le pipeline **reprend** à
  l'étape développeur sous le **même run** et va jusqu'au bout.
- **Refuser** : `/deny <run_id> [raison]` (ou le bouton) → le pipeline **s'arrête**,
  aucun code n'est écrit.

Pour relire le plan avant de décider : `/steps <run_id>` (ce que les agents ont
fait) ou la note du run dans le vault Obsidian.

> Le checkpoint ne se déclenche pas en `--simulate` côté CLI direct mais bien sur
> un vrai run. Pour ajouter un point de validation ailleurs, pose `pause_before: true`
> sur l'étape voulue dans `pipelines.yaml`.


## Pipeline `company-v2` (planification réordonnée + checkpoint)

Variante de `company` où la **sécurité et la synthèse passent avant le dev**, avec
ton checkpoint de plan après la synthèse :

**Phase 1 — Planification → checkpoint**
1. Aliénor (CEO) — débat bi-modèle (2 propositions + synthèse)
2. Blaise (CTO) — architecture (bi-modèle)
3. Hugo (CISO) — sécurité de l'architecture (bi-modèle)
4. Jules (Chief of Staff / CSO) — **concatène CEO+CTO+CISO et produit la proposition**
5. ⏸️ **checkpoint** — tu valides la proposition (Approve/Deny) avant le dev

**Phase 2 — Développement → PR**
6. Gustave (Dev) → 7. Victor (Reviewer, ouvre la PR) → 8. Hugo (CISO, clearance du code)
→ 9. Marie (QA) → 10. Théo (Documentation) → 11. Jules (**check final + approbation de la PR**)

Lancer : `hivepilot run-pipeline noxys-api company-v2` (ou `/runpipeline` dans Telegram).
Le `company` original reste disponible inchangé.


## Henri — l'auditeur externe

**Henri** est un méta-agent (hors pipeline) qui observe les cycles et aide les
autres agents à s'améliorer. Il tourne sur **Mistral (runner `vibe`)** et **ne
modifie jamais un prompt lui-même — il propose, tu approuves**.

- **Après chaque cycle (auto)** : Henri écrit une courte observation du run dans
  le vault Obsidian (`Audit/observation-run-<id>.md`). Désactivable via
  `HIVEPILOT_AUDITOR_AUTO=false`.
- **Audit profond (à la demande)** : `hivepilot audit noxys-api --deep` → Henri
  propose des diffs concrets sur `prompts/agents/*.md` (`Audit/proposal-latest.md`).
- **Observer un run précis** : `hivepilot audit noxys-api --run-id <id>`.

> Nécessite `vibe` installé (`pip install mistral-vibe` + `MISTRAL_API_KEY`).


## Groupes : projet `noxys` ↔ composants (E1)

Un **groupe** = un produit fait de plusieurs dépôts. `noxys` regroupe ses 24
composants (`noxys-api`, `noxys-web`, …), avec un `hub` (le dépôt où tournera la
planif au niveau groupe, à partir de E2). Config : `groups.yaml`.

- Lister : `hivepilot groups`
- **Tâche unique fan-out** : `hivepilot run noxys lint` → la tâche tourne sur tous
  les composants du groupe.
- **Pipeline sur groupe (E2)** : `hivepilot run-pipeline noxys company-v2` → la
  **planification tourne une seule fois dans le hub** (avec le manifeste des
  composants en contexte), puis la **phase 2 (dev → … → PR) fan-out sur les
  composants**. Le checkpoint de plan se trouve entre les deux.
  Les **agents choisissent les composants impactés** : Jules termine sa synthèse
  par une ligne `COMPONENTS: …` ; le fan-out de la phase 2 ne cible que ce
  sous-ensemble (affiché dans le checkpoint). À défaut de ligne, tous les
  composants sont ciblés.


## Agents sur machines distantes (SSH)

Chaque agent peut tourner sur une **autre machine** : on associe un `host` (alias
`~/.ssh/config` ou `user@machine`) à un rôle, et son CLI est exécuté via
`ssh <host> 'cd <repo> && <cli>'` au lieu d'en local.

```yaml
# policies.yaml (override par projet) ou roles.py (défaut)
role_overrides:
  ceo:            { host: machineA }
  chief_of_staff: { host: machineA }   # CSO
  cto:            { host: machineB }
  developer:      { host: machineC }
```

- **Auth** : on s'appuie sur le `~/.ssh/config` + clés/agent de l'opérateur
  (rien de secret stocké dans HivePilot), avec `BatchMode=yes` (pas de prompt).
  Options ssh additionnelles via `HIVEPILOT_SSH_OPTIONS`.
- **Prérequis** sur l'hôte distant : le CLI de l'agent installé + authentifié, et
  le dépôt cloné au **même chemin**.
- La sortie de l'agent distant est capturée et remonte comme en local (stream /
  interactions / Obsidian).
- `host` absent → exécution locale (comportement par défaut, inchangé).


## Versionner le Vault Obsidian (auto-commit)

Par défaut HivePilot **écrit** les notes (plans, ADR, synthèses) dans le Vault mais
ne les commit pas. Avec `HIVEPILOT_AUTO_COMMIT_VAULT=true`, après un run de pipeline
**réel** (`--no-dry-run`), HivePilot fait un **`git add`/`commit`/`push` du Vault**
(seules les modifs du Vault sont mises en index). Best-effort : si le Vault n'est
pas un dépôt git ou n'a aucun changement, c'est un no-op silencieux.

> Rappel : les écritures vault ne se font qu'en `--no-dry-run` ; les étapes de
> planification (CEO/CTO/CISO/Jules) ne touchent aucun dépôt de **code**.
