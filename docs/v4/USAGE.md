# HivePilot V4 — Usage (CLI & Telegram)

> For production deployment (install, services, secrets, quota, sandbox, multi-tenant, Postgres, observability): see [RUNBOOK.md](RUNBOOK.md).

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

### Typical run

```bash
hivepilot run-pipeline acme company --simulate          # validate wiring (no calls, no approval)
hivepilot run-pipeline acme company --auto-git          # real run -> queued for approval
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

See [ARCHITECTURE.md](ARCHITECTURE.md), [AGENTS.md](AGENTS.md), [CONFIG.md](CONFIG.md), [DEPLOYMENT-EXAMPLE.md](DEPLOYMENT-EXAMPLE.md).


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

## Cibler une étape sur un sous-ensemble (`only_components` / `only_tags`)

Une étape de pipeline peut être **restreinte à un sous-ensemble** des composants
touchés par le run, et peut aussi **désactiver le fail-fast**. Les trois champs
sont optionnels : une étape qui n'en pose aucun se comporte comme avant.

- `only_components: [nom, …]` — limite l'étape à ces composants (par nom).
- `only_tags: [tag, …]` — limite l'étape via des tags, résolus par la map `tags`
  du groupe dans `groups.yaml`.
- `continue_on_failure: true` — une étape qui échoue **n'arrête pas** le run (le
  fail-fast est neutralisé, le pipeline passe à l'étape suivante). Absent ou
  `false` = comportement fail-fast actuel.

**Skip :** l'ensemble cible = `only_components` ∪ (composants résolus depuis
`only_tags`). L'étape est **sautée si et seulement si** cet ensemble est non vide
**et disjoint** des composants réellement touchés par le run. Une étape sans
aucun sélecteur tourne toujours. Une étape sautée n'appelle pas sa tâche, n'est
pas comptée comme un échec, et laisse le contexte précédent intact.

**Fail-closed :** un `only_tags` absent des `tags` du groupe lève une `ValueError`
claire **d'entrée** (au chargement, avant toute étape) — un tag inconnu n'est
jamais silencieusement sauté.

## Routage du contexte inter-étapes (`inputs`/`outputs`, `context_routing_mode`)

Chaque rôle (`roles.yaml`) déclare `inputs` (les clés qu'il attend des étapes
précédentes) et `outputs` (les clés sous lesquelles sa sortie est rangée), ex.
`developer` : `inputs: [technical_spec, architecture_docs, codebase_context]`,
`outputs: [implementation, test_suite, implementation_notes]`.

`HIVEPILOT_CONTEXT_ROUTING_MODE` (`.env`, `full` | `keyed`, défaut `full`)
contrôle comment le `prior_context` (le texte transmis à l'étape suivante)
est construit :

- **`full` (défaut)** — comportement historique, inchangé : chaque étape
  reçoit le contexte agrégé de **toutes** les étapes précédentes
  (`HIVEPILOT_PRIOR_CONTEXT_MODE=full|synthesis|cap`). Les `inputs`/`outputs`
  déclarés n'ont **aucun effet** sur le contexte transmis dans ce mode.
- **`keyed` (opt-in)** — une étape dont le rôle déclare des `inputs` non-vides
  reçoit un contexte assemblé **seulement** à partir de ces clés, extraites
  de la sortie des étapes qui les produisent (`outputs`).

**Convention de section `## <CLÉ>`.** Une étape qui produit une sortie peut
découper précisément ses `outputs` en émettant des titres Markdown `##`
correspondant à chaque clé (insensible à la casse, `_`/`-`/espaces
normalisés), ex. pour `outputs: [technical_spec, adr]` :

```markdown
## TECHNICAL_SPEC
... contenu ...

## ADR
... contenu ...
```

**Fallbacks (toujours conservateurs — jamais de contexte vide silencieux) :**
- **Pas de section `## <CLÉ>` trouvée** → toute la sortie brute de l'étape
  est utilisée comme valeur de cette clé (fallback "whole-blob").
- **En mode `keyed`, toutes les clés `inputs` d'une étape sont absentes** du
  store → bascule automatique sur le contexte complet (`full`), avec un
  warning loggé listant les clés manquantes. Si seulement **certaines**
  clés manquent, le contexte `keyed` est construit avec celles présentes
  (pas de fallback dans ce cas).

**`can_block` est indicatif seulement.** Ce champ de `roles.yaml` documente
qu'un rôle est *censé* pouvoir bloquer un run (ex. `cto`, `reviewer`,
`ciso`), mais ne contrôle rien au runtime. Le comportement réel
(fail-fast ou non) est piloté par `continue_on_failure` **au niveau de
l'étape** dans `pipelines.yaml` (voir "Cibler une étape" ci-dessus), qui
prime sur `can_block`.

**Validation.** `hivepilot config validate` détecte les *dangling inputs*
(une clé `inputs` qu'aucune étape précédente du même pipeline ne produit
dans ses `outputs`) : simple **warning** en mode `full` (n'échoue pas la
validation — beaucoup de rôles ont des `inputs` "cosmétiques" fournis en
externe), mais **erreur bloquante** en mode `keyed` (une clé manquante y
dégrade réellement le contexte transmis).


## Pipeline `default` (planification réordonnée + checkpoint)

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

Lancer : `hivepilot run-pipeline acme-api default` (ou `/runpipeline` dans Telegram).
Le `company` original reste disponible inchangé.


## Direct agent orders (Telegram)

Address **one agent directly** — no full pipeline, no CEO → … → Docs chain.

### Generic `/ask`

```
/ask <agent> [@target] <order…>
```

- `<agent>` — role key or any alias (case-insensitive, accent-insensitive)
- `[@target]` — optional `@project` / `@group` (defaults to `HIVEPILOT_DEFAULT_TARGET`, default `acme`)
- `<order…>` — the instruction forwarded to the agent

```
/ask gustave @acme-api add unit tests for the auth module
/ask cto review the new schema proposal
/ask aliénor kickoff sprint 4
```

### Per-agent alias commands

Each agent has its own shortcut command — no need to type the agent name:

| Command | Agent | Full name |
|---|---|---|
| `/ceo` or `/alienor` | Aliénor | CEO |
| `/cos` or `/jules` | Jules | Chief of Staff |
| `/cto` or `/blaise` | Blaise | CTO |
| `/dev`, `/developer`, `/gustave` | Gustave | Developer |
| `/review`, `/reviewer`, `/victor` | Victor | Reviewer |
| `/ciso` or `/hugo` | Hugo | CISO |
| `/qa` or `/marie` | Marie | QA |
| `/docs`, `/documentation`, `/theo` | Théo | Documentation |
| `/audit` or `/henri` | Henri | Auditor (graceful reply — see below) |

All alias commands accept `[@target] <order…>`:

```
/gustave @acme-api add unit tests for the auth module
/docs @acme-web update the API reference
/theo write the changelog entry for v1.2
```

### Henri (Auditor) — ad-hoc limitation

Henri (`/audit`, `/henri`, `/ask henri`) replies gracefully when invoked directly:
`"Henri (Auditor) runs automatically after each cycle; ad-hoc audit not wired yet."`

Henri still runs automatically after each pipeline cycle (disable with
`HIVEPILOT_AUDITOR_AUTO=false`). Deep audits remain available via the CLI:
`hivepilot audit <project> --deep`.

### Default target

Set `HIVEPILOT_DEFAULT_TARGET` to change the project/group used when no `@target`
is given (default: `acme`).

---

## Henri — l'auditeur externe

**Henri** est un méta-agent (hors pipeline) qui observe les cycles et aide les
autres agents à s'améliorer. Il tourne sur **Mistral (runner `vibe`)** et **ne
modifie jamais un prompt lui-même — il propose, tu approuves**.

- **Après chaque cycle (auto)** : Henri écrit une courte observation du run dans
  le vault Obsidian (`Audit/observation-run-<id>.md`). Désactivable via
  `HIVEPILOT_AUDITOR_AUTO=false`.
- **Audit profond (à la demande)** : `hivepilot audit acme-api --deep` → Henri
  propose des diffs concrets sur `prompts/agents/*.md` (`Audit/proposal-latest.md`).
- **Observer un run précis** : `hivepilot audit acme-api --run-id <id>`.

> Nécessite `vibe` installé (`pip install mistral-vibe` + `MISTRAL_API_KEY`).


## Groupes : projet `acme` ↔ composants (E1)

Un **groupe** = un produit fait de plusieurs dépôts. `acme` regroupe ses 24
composants (`acme-api`, `acme-web`, …), avec un `hub` (le dépôt où tournera la
planif au niveau groupe, à partir de E2). Config : `groups.yaml`.

Un groupe peut aussi définir des **`tags`** (`dict[str, list[str]]`, défaut `{}`)
qui nomment un sous-ensemble de composants ; une étape de pipeline avec
`only_tags` résout ses composants via cette map (cf. `only_tags` plus haut).

- Lister : `hivepilot groups`
- **Tâche unique fan-out** : `hivepilot run acme lint` → la tâche tourne sur tous
  les composants du groupe.
- **Pipeline sur groupe (E2)** : `hivepilot run-pipeline acme default` → la
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


## Dedicated stream channel (Telegram)

By default the live agent stream and the approval/notification messages share one
chat. Set a **dedicated channel** for the agent conversation so it stays separate
from your control/approval chat:

```bash
export HIVEPILOT_TELEGRAM_STREAM_CHAT_ID=-100xxxxxxxxxx   # a channel/group id
```

- Live agent turns (each labelled with the agent + role) -> this channel.
- Approvals, run start/result notifications -> the main notification chat.
- Unset -> stream falls back to the notification chat (unchanged behaviour).

> Gives the "agent conversation in its own channel" effect with a single bot — no
> need for one bot per role (each message already carries the agent name + role).


## Free-text @mentions

The bot accepts plain (non-slash) messages starting with `@`:

| Syntax | Effect |
|--------|--------|
| `@gustave fix auth bug` | Run Gustave (Developer) on the default project |
| `@blaise @acme-api review API` | Run Blaise (CTO) on the `acme-api` project |
| `@acme ship device-fleet API` | Launch `default` pipeline on the `acme` group |
| `@acme-api implement X` | Launch `default` pipeline on project `acme-api` |

Resolution priority: group > agent > project. So if `acme` is both a group and a project, it routes to the group.

### BotFather privacy mode (IMPORTANT for group chats)

In **group chats**, Telegram's default privacy mode makes the bot ignore non-command messages. To receive `@mention` messages in a group:

1. Open BotFather → `/setprivacy`
2. Select your bot
3. Choose **Disable**

In **1:1 DMs and channels**, the bot receives all messages regardless of privacy mode.
