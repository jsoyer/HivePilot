# HivePilot V4 — Usage (CLI & Telegram)

> For production deployment (install, services, secrets, quota, sandbox, multi-tenant, Postgres, observability): see [RUNBOOK.md](RUNBOOK.md).

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e .          # lightweight core
.venv/bin/pip install -e ".[notifications]"                 # + Telegram bot
.venv/bin/pip install -e ".[langchain]"                     # + RAG (langchain+torch, optional)
.venv/bin/pip install -e ".[dashboard]"                     # + Textual dashboard
```

`hivepilot doctor` checks paths, external binaries, the agent runner CLIs
referenced by your configured tasks, and the **mandatory agent CLI**
verdict — at least one of `claude` / `codex` / `vibe` must be on PATH to run
a pipeline (`claude` is the strongest/most-tested prerequisite). See
[PLUGINS.md](PLUGINS.md#agent-runner-taxonomy-built-in-vs-plugin) for the
full built-in vs. plugin agent taxonomy and PATH-activation rule.

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
| `hivepilot dashboard` | Mirador — tabbed Textual TUI (needs `[dashboard]`) |
| `hivepilot telegram` | Start the Telegram bot (polling; needs `[notifications]` + token) |
| `hivepilot doctor` | Environment / readiness check |

**Dry-run vs simulate:** `--dry-run` (default true) only skips *vault writes*;
the agents still run. `--simulate` skips *agent execution* entirely (safe preview).

### Mirador — the dashboard's tabbed layout

`hivepilot dashboard` (Textual TUI, needs `[dashboard]`) launches **Mirador**,
a tabbed, read-only insight dashboard (the `hivepilot dashboard` command name
itself is unchanged — "Mirador" is just the app's title/branding). Four tabs
(`r` refreshes the active data, `q` quits):

- **Analytics** — runs, the Metrics table (totals, outcome counts, and
  p50/p95/p99 run duration), a step-failure-hotspots table (the
  `(step, status)` combinations with the most failures, from
  `analytics_service.step_failure_hotspots()`), and recent interactions.
- **Cost** — overall cost (USD) and input/output token totals across all
  recorded steps, an `unpriced` coverage count (steps with no self-reported
  cost and no price-map match, so the total is never presented as silently
  complete), and a per-provider/per-model breakdown. Reuses
  `hivepilot.services.analytics_service.cost_summary()` — see
  [RUNBOOK.md](RUNBOOK.md) ("Cost analytics") for the pricing table and cost
  precedence rules.
- **Health** — plugin health (name / status / detail), read via
  `PluginManager.check_all()` — the same never-raise health check every
  `plugins health`/`plugins list` CLI command uses. A broken health check
  renders as `error`, it never crashes the dashboard.
- **Mem0** — recent memories (with their typed provenance metadata —
  category/project/task/timestamp) when the [mem0 plugin](PLUGINS.md) is
  configured (`HIVEPILOT_MEM0_ENABLED=true`) and reachable; otherwise a clear
  "not configured" placeholder. Never crashes, never shows a secret/token.

All tabs are unscoped/unbounded (tenant=None, days=None) — Mirador is a local
operator tool, like `hivepilot plugins list`.

### Typical run

```bash
hivepilot run-pipeline acme company --simulate          # validate wiring (no calls, no approval)
hivepilot run-pipeline acme company --auto-git          # real run -> queued for approval
hivepilot approvals                                      # see the pending run
# approve via CLI or Telegram, then agents execute; developer opens a PR you merge
```

### Execution mode: `cli` vs `api`

Every pipeline — and each of its stages — has an execution `mode` that
decides whether its agent-capable runners are driven through their **CLI
binary** (`mode: cli`, the default — byte-identical to pre-`mode`
behaviour) or through the provider's own **HTTP API** (`mode: api` —
`claude` currently routes `mode: api` through the Anthropic Messages API).

`PipelineConfig.mode` sets the pipeline-wide default (`"cli"` if omitted); an
individual `PipelineStage.mode` overrides it for that stage only.
Precedence is **stage > pipeline > `"cli"`** (`hivepilot.models.resolve_mode`):

```yaml
# pipelines.yaml
company:
  description: "..."
  mode: cli               # pipeline-wide default (cli is also the implicit default if omitted)
  stages:
    - name: plan
      task: ceo-intake      # inherits the pipeline default -> cli
    - name: draft-docs
      task: documentation
      mode: api             # this stage's agent-capable runners go through the API instead
```

A runner kind that doesn't support the resolved mode fails **before any
subprocess/request is made** (`RunnerModeUnsupportedError`, fail-closed) —
e.g. routing a `mode: api` stage at a CLI-only runner like `shell`. Every
agent-capable runner (`claude`, plus every `PromptCliRunner`-based kind —
`codex`/`vibe` and every plugin agent kind from
[PLUGINS.md](PLUGINS.md#agent-runner-taxonomy-built-in-vs-plugin)) supports
`{"cli", "api"}`; every non-agent runner supports `{"cli"}` only; and
`openrouter` is the one deliberate exception — **API-only**
(`supported_modes == {"api"}`, it has no CLI binary at all, so `mode: cli`
on `openrouter` fails the same way).

### Model + reasoning effort (`model` / `effort`)

Both a pipeline and each of its stages can *declare* a `model` (a plain
string, runner-specific) and an `effort` — a closed set of reasoning-effort
levels: `low` | `medium` | `high` | `xhigh` | `max`
(`hivepilot.models.EffortLevel`). These raw fields — `PipelineStage.model` /
`.effort` and `PipelineConfig.model` / `.effort` — both default to `None`; a
pipeline/stage that sets neither dispatches byte-identically to before these
fields existed. The **effective** model/effort actually handed to a runner
is never read straight off these raw fields — it is produced by a two-stage
resolution:

1. **Pipeline vs. stage.** `hivepilot.models.resolve_stage_model` /
   `resolve_effort` resolve the raw `stage.model`/`.effort` against the raw
   `pipeline.model`/`.effort` (`stage > pipeline`, mirroring `mode`'s own
   `resolve_mode` precedence), producing a *stage-resolved* value that is
   still `None` if neither field was set anywhere.
2. **Role vs. policy.** `hivepilot.roles.resolve_stage_dispatch` takes that
   stage-resolved value (as its `stage_model`/`stage_effort` arguments) and
   layers it into the full precedence chain below to produce the final,
   effective dispatch model/effort:

```
policy.role_overrides  >  stage (pipeline-resolved)  >  role  >  runner-default
```

A per-project policy's `role_overrides[role].model` / `.effort` (see
[CONFIG.md](CONFIG.md)) always wins over both the stage-resolved value and
the role's own binding — it is the security control that must never be
short-circuited by a stage or role author.

```yaml
# pipelines.yaml
company:
  description: "..."
  effort: high              # pipeline-wide default reasoning effort
  stages:
    - name: draft
      task: developer       # role "developer" -> claude; inherits effort: high
      model: claude-opus    # stage-level model override, this stage only
      effort: low            # stage-level effort override, this stage only
    - name: review
      task: reviewer         # role "reviewer" -> codex, role.model "gpt-5.5"
                              # no stage model/effort set -> falls back to the
                              # role's own model ("gpt-5.5") and the pipeline
                              # default effort ("high")
```

**Per-runner effort mapping.** `effort` is an internal HivePilot concept —
each runner maps it (or ignores it) on its own:

- **Claude** (`ClaudeRunner`) injects the resolved level as the
  `MAX_THINKING_TOKENS` environment variable on the `claude` subprocess
  (`hivepilot.runners.claude_runner.EFFORT_TOKEN_MAP`): `low` → 4000,
  `medium` → 12000, `high` → 24000, `xhigh` → 40000, `max` → 63999. When no
  effort resolves anywhere in the chain, `MAX_THINKING_TOKENS` is left unset
  (byte-identical to a pre-`effort` run). This is a real reasoning-effort
  knob, **not** a no-op.
- **Codex** (`CodexRunner`) maps it to the `-c model_reasoning_effort=<level>`
  CLI flag; when no effort resolves anywhere in the chain, it still defaults
  to `medium` (byte-identical to the pre-`effort`-field hardcoded flag).
  `xhigh` is passed through literally.
- **Every other runner** (`gemini`/`opencode`/`cursor`/`vibe`/`ollama` and the
  other prompt-cli plugin kinds, `shell`, `container`, IaC runners, etc.) has
  no reasoning-effort concept and treats `effort` as a safe no-op — the value
  is read but never turned into an argument or env var.

See [CONFIG.md](CONFIG.md#reasoning-effort) for the same mapping from the
config angle and [RUNBOOK.md](RUNBOOK.md) for the ops-facing table.

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


## Debate judge, challenge arbiter & the fail-closed PR gate (opt-in)

Two independent, **opt-in** LLM adjudicators can sit on top of the CEO debate
and the reviewer/developer challenge/rebuttal flow, and — once either is
enabled — a **fail-closed** gate governs `promote_pr`/`merge_pr`. Both flags
default off; with both off, behaviour is byte-identical to the pre-existing
templated/self-adjudicated paths.

| Setting | Env var | Default |
|---|---|---|
| `enable_debate_judge` | `HIVEPILOT_ENABLE_DEBATE_JUDGE` | `false` |
| `judge_runner` | `HIVEPILOT_JUDGE_RUNNER` | `claude` |
| `judge_model` | `HIVEPILOT_JUDGE_MODEL` | *(none — runner default)* |
| `enable_challenge_arbiter` | `HIVEPILOT_ENABLE_CHALLENGE_ARBITER` | `false` |
| `judge_confidence_threshold` | `HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD` | `0.5` |

**The flow:**

- **Debate judge** (`enable_debate_judge`) — after a CEO debate's model
  positions are collected, one extra LLM call (`judge_runner`/`judge_model`,
  default the `claude` runner) scores the debate into a real
  `decision` + `confidence`, replacing the templated "Synthesis of N model
  proposals…" text that gets written into the ADR. A malformed, empty, or
  unparseable judge response is **never fabricated into a decision** — it
  silently falls back to the templated/majority-stance path instead.
- **Challenge arbiter** (`enable_challenge_arbiter`) — when a reviewer
  challenges the developer (or any target/challenger pair) mid-pipeline, a
  **third, neutral role** (never the challenger, never the target) adjudicates
  the rebuttal `ACCEPT`/`DEFEND`, instead of letting the challenger self-grade
  its own resolution.

**Fail-closed PR-gate semantics:** once either flag is on, `promote_pr` and
`merge_pr` require an explicit approval verdict (`ACCEPT`/`ACCEPTED`/
`APPROVE`/`APPROVED`, case-insensitive) at `confidence >= judge_confidence_threshold`.
Anything else — an absent verdict, an empty/unparseable decision, a
non-approval decision, or a missing/low confidence — **blocks** promotion.
`create_pr` is **never gated**, so a human can always see the (draft) PR and
its report even when promotion is blocked.

**Human escalation is always preserved.** `MAINTAIN`/`DEFEND`, low confidence,
or an arbiter call that errors out all route to 🙋 human escalation
(`stream_needs_human`) — the judge/arbiter never silently overrules a human,
and `NEEDS_HUMAN` stays first-class blocking for the PR gate.

**Worked example:**

```bash
export HIVEPILOT_ENABLE_DEBATE_JUDGE=true
export HIVEPILOT_ENABLE_CHALLENGE_ARBITER=true
export HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD=0.7
```

- Arbiter returns `{"decision": "ACCEPT", "confidence": 0.9}` → resolved, no
  human ping, and (if this was the run's governing verdict) `promote_pr`
  proceeds.
- Arbiter returns `{"decision": "DEFEND", "confidence": 0.95}` → escalates to
  🙋 human review, and `promote_pr`/`merge_pr` are skipped for this run even
  if every other stage succeeded.

**Per-pipeline opt-in via `debate:`.** The flags above are the fleet-wide
floor; an individual `pipelines.yaml` pipeline (or one of its stages) can
additionally opt itself into debate/consensus via a `debate:` block, without
touching global config — see "Debate / consensus per-pipeline override" in
[CONFIG.md](CONFIG.md) for the full field reference. The enable semantics
are **OR, strengthen-only**: a pipeline can turn the judge/arbiter **ON**
even when the global floor is off, but a pipeline (or stage) can **never**
turn OFF a gate the operator mandated globally — a `debate: {enable_judge:
false}` in a pipeline is simply ignored when `enable_debate_judge=true`
fleet-wide. This is the same fail-closed guarantee as the global flags
themselves: no `debate:` block can weaken the gate below the operator's
floor, and `confidence_threshold` is validated to `(0, 1]` at config-load
time — `0` or an absent-but-required threshold never silently disables the
gate.

**Worked example — one pipeline opted further in, one left untouched:**

```yaml
# pipelines.yaml
pipelines:
  release:
    description: "high-stakes release pipeline — extra scrutiny"
    debate:
      enable_judge: true
      enable_arbiter: true
      confidence_threshold: 0.8   # stricter than the fleet-wide default
    stages:
      - name: plan
        task: ceo-plan
      - name: review
        task: developer-review

  docs-only:
    description: "low-stakes docs pipeline — no debate: block at all"
    stages:
      - name: write-docs
        task: documentation-task
```

With the global floor at its defaults (`enable_debate_judge=false`,
`enable_challenge_arbiter=false`), `release` runs with the judge and arbiter
both active at `confidence_threshold=0.8`, while `docs-only` behaves exactly
as if this whole feature didn't exist — no judge call, no arbiter call, no
gate. Flip `HIVEPILOT_ENABLE_CHALLENGE_ARBITER=true` fleet-wide and
`docs-only` gains the arbiter too (the floor always applies), while
`release`'s own `confidence_threshold=0.8` keeps governing its own gate
regardless.

See [ARCHITECTURE.md](ARCHITECTURE.md), [CONFIG.md](CONFIG.md).

## Auto-learning lessons loop (opt-in)

HivePilot can distill a completed run's verdicts/interactions/outcomes into
reusable **lessons**, validate each candidate against the run's REAL outcome
(never an LLM's own self-report), and inject only the validated ones into a
future run's prompt — closing a "learn from what actually happened" loop
around the pipeline. Fully **opt-in**: every flag below defaults off/dormant,
and the flags-off path is byte-identical to a HivePilot build that predates
this feature entirely (no extra LLM call, no `lessons` rows, no prompt
section).

**Flags:**

| Setting | Env var | Default |
|---|---|---|
| `enable_lesson_distillation` | `HIVEPILOT_ENABLE_LESSON_DISTILLATION` | `false` |
| `lesson_distill_runner` | `HIVEPILOT_LESSON_DISTILL_RUNNER` | `claude` |
| `lesson_distill_model` | `HIVEPILOT_LESSON_DISTILL_MODEL` | *(none — runner default)* |
| `lesson_min_score` | `HIVEPILOT_LESSON_MIN_SCORE` | `0.5` |
| `lesson_inject_limit` | `HIVEPILOT_LESSON_INJECT_LIMIT` | `5` |
| `enable_semantic_lesson_retrieval` | `HIVEPILOT_ENABLE_SEMANTIC_LESSON_RETRIEVAL` | `false` |

**The flow — distill → validate → inject:**

1. **Distill** (`enable_lesson_distillation`) — at the end of each project's
   task run, if the run produced at least one judge/arbiter verdict or agent
   interaction (an outcome-only run with neither is skipped — not worth a
   costed LLM call), ONE extra LLM call (`lesson_distill_runner`/
   `lesson_distill_model`, default the `claude` runner) reviews the run's
   verdicts, interactions, and outcome and proposes zero or more concise,
   reusable lesson candidates as `text`/`category`. A malformed, empty, or
   unparseable response — or a response with no usable `text` — **never
   fabricates a lesson**: nothing is persisted on a doubt.
2. **Validate (anti-poisoning gate)** — each candidate is immediately scored
   against the SAME run's REAL outcome signal, never the distiller's own
   self-report (any `score`/`confidence` an LLM includes in its distillation
   response is parsed out and discarded — this module doesn't even read
   those keys). The score is the max of whichever real signals actually
   fired:
   - the run itself succeeded (`RunResult.success=True`) → `1.0`
   - a `"challenge"`-kind verdict was genuinely `ACCEPT`ed at/above
     `lesson_min_score` (a `MAINTAIN`/`DEFEND` verdict, or an `ACCEPT` below
     the floor, contributes nothing) → `1.0`
   - that verdict's own `confidence`, when finite and in `[0, 1]`
   A lesson is marked `validated=1` only when this real-signal score is
   `>= lesson_min_score`. **Fail-closed**: an absent or empty outcome signal
   (no success, no resolved challenge, no confidence at all) is treated as
   DENY, never as "no constraint → allow" — every candidate is persisted
   (so nothing is silently lost), but an unvalidated candidate can NEVER be
   retrieved or injected; there is no toggle that surfaces it. `lesson_min_score`
   itself is validated at startup to a finite value in `(0.0, 1.0]` — a
   misconfigured `0` floor can't silently admit every candidate.
3. **Inject** — a future run for the same `project`/`role`/`task` retrieves
   only `validated=1` lessons, ranked by score (desc) then recency (desc),
   capped at `lesson_inject_limit`, and renders them as a stable
   `Lessons learned:` section in the agent's prompt — next to (not inside)
   the existing `Knowledge context` section, for both the `claude` and
   `api`/prompt-CLI runners. With `enable_lesson_distillation=false` (the
   default), this section is never even queried — the prompt is
   byte-identical to a lessons-loop-free build.

**Redaction guarantee.** Secrets are masked before a lesson is ever
persisted, and separately before the distillation prompt ever leaves the
process (the egress choke point) — a resolved `${secret:NAME}` value sitting
in a step's failure `detail` (which, unlike verdict/interaction summaries,
is not pre-redacted upstream) is stripped from the FULL assembled prompt
immediately before the `lesson_distill_runner` call, and the raw response is
redacted again before being parsed and stored. **Residual limitation**
(shared with every other sink in HivePilot): a plaintext secret sitting in
an env value that was never registered via `${secret:NAME}` is not tracked
by the redaction registry and can't be masked — same limitation as the
`mem0`/`obsidian` store hooks and every other egress point in this codebase.
Register real secrets through `${secret:NAME}` (see the Secrets sections in
[CONFIG.md](CONFIG.md)) to get masking coverage.

**Semantic retrieval is opt-in with a dependency-free SQLite fallback.**
With `enable_semantic_lesson_retrieval=false` (the default), retrieval is a
plain SQLite `score DESC, created_at DESC` read — no `langchain`/
`sentence-transformers`/FAISS import anywhere on this path, so the core loop
works with the optional `hivepilot[langchain]` extra completely absent. With
the flag on, retrieval additionally re-ranks the ALREADY-VALIDATED candidate
pool by embedding similarity (same optional embedding backend
`knowledge_service`'s RAG path uses, lazy-imported) blended with the real
validation score — it only ever reorders validated rows, it never widens the
pool to admit an unvalidated one. Any failure on this path — the extra not
installed, an embedding-call error, anything — silently falls back to the
same plain SQLite ranking; this flag can never turn a working retrieval into
a crash.

See [CONFIG.md](CONFIG.md) for the flag reference table.

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

**Entrées optionnelles (`optional_inputs`).** Un rôle peut aussi déclarer
`optional_inputs: [clé, ...]` — une liste **séparée** de `inputs` (pas un
sous-ensemble marqué). En mode `keyed`, ces clés sont routées comme
`inputs` quand une étape en amont les produit, mais ne sont **jamais**
considérées comme manquantes (ni pour le fallback, ni pour la validation
ci-dessous) si aucune étape ne les produit dans ce pipeline. Cas d'usage :
un rôle partagé entre plusieurs pipelines qui ne consomme une clé (ex.
`design_spec`) que lorsqu'une étape de design en amont existe.

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
dégrade réellement le contexte transmis). Les clés `optional_inputs` d'un
rôle sont exemptées de ce contrôle dans les deux modes.


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
