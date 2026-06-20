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
