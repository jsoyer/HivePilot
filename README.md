# 🐝 HivePilot v2

HivePilot est un cockpit AI pour orchestrer des workflows multi‑repos avec des agents Claude, LangChain, CrewAI, LangGraph, des runners shell et des automatisations GitHub/Git. Cette version introduit un mode interactif, l’exécution parallèle, une journalisation structurée persistante et des sorties d’exécution standardisées.

---

## ✨ Fonctionnalités clés

- **Mode interactif** (`hivepilot interactive`) basé sur Questionary pour sélectionner projets, tâches, pipelines ou lancer les templates `gh-*`.
- **Parallelisme** configuré (ThreadPoolExecutor) pour diffuser une tâche/pipeline sur plusieurs dépôts en simultané (`--concurrency`, `.env`).
- **Runners modulaires** (Claude, shell, LangChain, internal/GitHub, futur CrewAI) déclarés dans `tasks.yaml` via `runner_ref`.
- **Support natif des CLIs** `codex`, `gemini-cli`, `opencode`, `ollama` pour varier les moteurs LLM locaux/distants.
- **Switch CLI/API** paramétrable dans `tasks.yaml` (mode `cli` par défaut, `api` possible). Sonnet/Opus/Haiku sont pilotés via `model_profiles.yaml`.
- **Pipelines YAML** (`pipelines.yaml`) + compatibilité avec les pipelines existants.
- **Historique & logging** : structlog écrit un log JSON + `runs/<timestamp>/summary.json`. Chaque run produit un dossier standardisé.
- **Automatisation Git/GitHub** : services dédiés (`services/git_service.py`, `services/github_service.py`) et commandes CLI `hivepilot gh repo-init/issue/release` + tâches YAML `gh-*`.
- **Support LangGraph, LangChain, CrewAI, Textual** (extras installables) prêt pour les orchestrations avancées et dashboards temps-réel.

---

## 📂 Architecture

```
hivepilot/
├── hivepilot/
│   ├── cli.py                 # Typer CLI + mode interactif + commandes gh
│   ├── config.py              # Settings Pydantic (.env)
│   ├── orchestrator.py        # Scheduler, multi-projet, concurrency
│   ├── registry.py            # Résout les runners déclarés en YAML
│   ├── models.py              # Pydantic (projects, tasks, pipelines)
│   ├── pipelines.py           # Helpers pipelines
│   ├── runners/
│   │   ├── base.py
│   │   ├── claude_runner.py
│   │   ├── shell_runner.py
│   │   ├── langchain_runner.py
│   │   └── internal_runner.py
│   ├── services/
│   │   ├── git_service.py
│   │   ├── github_service.py
│   │   ├── project_service.py
│   │   └── pipeline_service.py
│   └── utils/
│       ├── io.py              # dossiers de runs + summary
│       ├── logging.py         # structlog
│       └── shell.py
├── prompts/
├── projects.yaml
├── tasks.yaml                 # runners + tâches
├── pipelines.yaml             # pipelines dédiés
├── model_profiles.yaml        # mapping Sonnet/Opus/Haiku
├── .env.example
├── requirements.txt
└── README.md
```

---

## ⚙️ Configuration YAML

### projects.yaml

```yaml
projects:
  example-api:
    path: ~/dev/example-api
    description: API demo
    claude_md: CLAUDE.md
    default_branch: main
    owner_repo: your-user/example-api
```

### tasks.yaml

```yaml
runners:
  claude-docs:
    kind: claude
    command: claude
    default_model: sonnet
    default_agent: docs-writer
  validation-suite:
    kind: shell
    command: |
      if [ -f package.json ]; then npm test || true; fi
      if [ -f pyproject.toml ]; then pytest || true; fi

tasks:
  docs:
    description: Rewrite README/docs
    steps:
      - name: rewrite docs
        runner: claude-docs
        prompt_file: prompts/docs_rewrite.md
        metadata:
          claude_profile: automation
    git:
      commit: true
      push: true
      create_pr: true

  codex-audit:
    description: Run Codex CLI on architecture prompt
    steps:
      - name: codex review
        runner: codex-default
        prompt_file: prompts/architecture_review.md

  gh-repo-init-task:
    description: Provision GitHub repo (commande interne)
    steps:
      - name: repo init
        runner: shell
        command: hivepilot gh repo-init {project_name} --set-remote --push
```

#### Templating Shell / CLI

Les commandes shell/CLI acceptent `{variables}` : `project_name`, `project_path`, `project_description`, `project_default_branch`, `project_owner_repo`, `task_name`, `step_name`, `extra_prompt`. Utilise `{{`/`}}` pour échapper.

#### Profils Claude (`model_profiles.yaml`)

```yaml
claude_profiles:
  coding:
    model: sonnet
  architecture:
    model: opus
  automation:
    model: haiku
```

Associe un profil via `metadata.claude_profile` (ex. `coding` pour du code). Le runner Claude sélectionne automatiquement Sonnet / Opus / Haiku suivant ton YAML. Tu peux ajouter tes propres profils (e.g. `review`, `summary`).

### CLI vs API runners

Chaque runner CLI peut basculer en mode API via `options.mode: api` ou `metadata.mode: api`. Exemple :

```yaml
  codex-default:
    kind: codex
    command: codex
    options:
      mode: api         # "cli" par défaut si absent
      api_provider: openai
      api_model: gpt-4o
```

- `api_provider`: `openai`, `anthropic`, `google`, `mistral`, `perplexity`, `openrouter`.
- `api_model`: nom exact du modèle pour l’API cible.
- En mode CLI, la commande (`command`) est exécutée telle quelle avec le prompt.

### API Keys / ENV

Déclare les clés dans ton shell ou `.env` avant d’utiliser `mode: api` :

| Provider     | Variable attendue      |
|--------------|------------------------|
| OpenAI       | `OPENAI_API_KEY`       |
| Anthropic    | `ANTHROPIC_API_KEY`    |
| Google Gemini| `GOOGLE_API_KEY`       |
| Mistral      | `MISTRAL_API_KEY`      |
| Perplexity   | `PERPLEXITY_API_KEY`   |
| OpenRouter   | `OPENROUTER_API_KEY`   |

Optionnellement `OPENAI_API_BASE` si tu utilises Azure/OpenRouter comme proxy OpenAI-compatible.

### pipelines.yaml

```yaml
pipelines:
  pentest-fix-review:
    description: Pentest -> refactor -> docs
    stages:
      - name: pentest
        task: pentest
      - name: refactor follow-up
        task: refactor
      - name: docs summary
        task: docs
```

---

## 🧑‍💻 Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

Extras :

```bash
pip install -e .[langgraph]
pip install -e .[crewai]
pip install -e .[full]        # tout LangGraph + CrewAI
```

Docker :

```bash
docker compose build
docker compose run --rm hivepilot hivepilot doctor
```

---

## 🕹 CLI

```bash
hivepilot doctor
hivepilot list-projects
hivepilot list-tasks
hivepilot run example-api docs
hivepilot run example-api docs --project example-site --concurrency 2
hivepilot run example-api pentest --all --auto-git
hivepilot run example-api gh-issue-from-extra --extra-prompt "Docs refresh"
hivepilot run example-api codex-audit
hivepilot run example-api gemini-brief
hivepilot run example-api opencode-fix
hivepilot run example-api ollama-scan
hivepilot run-pipeline example-api pentest-fix-review
hivepilot interactive

# GitHub helpers directs via services
hivepilot gh repo-init example-api --push
hivepilot gh issue example-api "Docs refresh" --body "Regénérer README"
hivepilot gh release example-api v0.2.0 --title "Docs refresh"
```

---

## 🤖 Logging & sorties

- `runs/<timestamp>/summary.json` décrivant chaque projet/étape + statut.
- Logs JSON (structlog) dans `runs/logs/hivepilot.log`.
- Option `.env` `HIVEPILOT_OUTPUT_FORMAT` (JSON/plain) pour les summaries.
- Support d’un dashboard Textual (activer `HIVEPILOT_ENABLE_TEXTUAL_UI=true`) prêt pour brancher un TUI en continu.

---

## 🧠 Engines et runners

- **Native** : LLM Claude + shell + registry interne.
- **LangGraph** : référence `graph: module:function` → compile/invoque le graph.
- **CrewAI** (via `internal_runner` + `tasks.yaml`) : très simple crew builder dans `workflows/`.
- **LangChain** : runner dédié capable de charger un `LLMChain`.

Ajoute tes propres runners en créant un fichier dans `hivepilot/runners/` puis en les enregistrant via le `registry`.

---

## 🐙 GitHub & Git

- `git_service.py` gère checkout/push/auto-git.
- `github_service.py` encapsule `gh repo/view/create`, issues et releases.
- Tâches YAML `gh-*` ou commandes `hivepilot gh ...` pour intégrer ces opérations dans tes pipelines.

---

## ✅ À tester

1. `hivepilot doctor`
2. `hivepilot run example-api docs --dry-run`
3. `hivepilot run example-api gh-repo-init-task`
4. `hivepilot run example-api gh-issue-from-extra --extra-prompt "Docs refresh"`
5. `hivepilot run-pipeline example-api pentest-fix-review --concurrency 2`

Chaque run doit laisser un dossier `runs/<timestamp>` avec logs + summary.
