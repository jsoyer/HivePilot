# HivePilot Roadmap

## Completed

- Phase 1 -- Approvals & Policy Hooks
- Phase 2 -- Artifact Publishing
- Phase 3 -- Knowledge Feedback
- Phase 4 -- RBAC & API Tokens
- Phase 5 -- ChatOps Integration
- Phase 6 -- Metrics & Dashboards
- Phase 7 -- Config Lint & Validation
- Phase 8 -- Containerized Runners
- Phase 9 -- GitHub Enhancements & Secrets
- Phase 10 / 10b -- Security Hardening
- Phase 11 -- Runtime Stability
- Phase 12 / 12b -- Test Suite & Continuous Audit
- Phase 13 -- Dependency Cleanup & Startup Performance
- Phase 14 / 14b / 14c / 14d / 14e -- Architecture, API Maturity, Config Repo, Proxy, Caddy
- Phase 15 -- Scheduler Resilience & Retry Queue
- Phase 23b -- Telegram Bot (dual-mode)

---

## Phase 10 -- Security Hardening (critical)

Priority: **immediate** -- blocks any production or shared deployment.

- [x] Fix command injection in shell runner (`templates.py` uses raw `format_map`)
  - Escape all interpolated values with `shlex.quote()` before shell execution
  - Validate volume mount paths in container runner
- [x] Fix CORS policy (`allow_origins=["*"]` + `allow_credentials=True`)
  - Restrict to configured origins or remove credentials
- [x] Authenticate ChatOps endpoints (missing `require_role` on `/chatops/*`)
- [x] Hash API tokens at rest (SHA-256) and compare with `hmac.compare_digest()`
  - Mask tokens in `hivepilot tokens list` output
- [x] Move Google API key from URL query string to request header
- [x] Add path validation to file-based secret resolver (whitelist allowed dirs)
- [x] Add `state.db` and `api_tokens.yaml` to `.gitignore`
- [x] Add rate limiting on `/run` and ChatOps endpoints

## Phase 10b -- Security Hardening (suite)

Priority: **high** -- vecteurs non couverts par la phase 10.

- [x] Token expiration (TTL configurable) + commande `hivepilot tokens rotate`
- [x] Limite de taille sur les champs libres (`extra_prompt`, notes) -- max 4096 chars
- [x] Hardening prompt injection -- validation et sanitisation de `extra_prompt` avant envoi au LLM
- [x] Audit log structuré : `token_hash` + action + résultat tracés sur chaque appel API authentifié
- [x] Ajouter `runs/` au `.gitignore` (logs et artefacts peuvent contenir des données sensibles)

## Phase 11 -- Runtime Stability (critical)

Priority: **immediate** -- several code paths crash at runtime.

- [x] Fix missing imports causing `NameError` at runtime
  - `knowledge_service.py`: `datetime`
  - `prompt_cli_runner.py`: `Any`
- [x] Wire `timeout_seconds` from task step config into `subprocess.run()` calls
- [x] Fix `run_approved` -- wrap `_execute_task` in try/except, set status to "failed" on error
- [x] Fix `git_service.py` -- clean repo should warn/no-op, not raise `RuntimeError`
- [x] Add pipeline fail-fast: stop on stage failure unless `continue_on_failure` is set
- [x] Guard multi-workers SQLite : détecter `--workers > 1` au démarrage uvicorn et avertir
- [x] `/health` réel : vérifier `state.db` accessible + runners configurés + dépendances optionnelles

## Phase 12 -- Test Suite Foundation

Priority: **high** -- no tests exist today.

- [x] Create `tests/` directory with `conftest.py` and pytest config
- [x] Security-critical path tests first:
  - Shell runner template rendering (injection prevention)
  - Token service (hashing, comparison, CRUD, expiry, rotation)
  - Secret resolver (env, file, path validation, path traversal)
  - Policy service (allow/deny logic, project overrides)
- [x] Runner tests (shell, container)
- [x] Orchestrator tests (pipeline fail-fast, run_approved error handling)
- [x] Git service tests (clean repo no-op, dirty repo commit/push)
- [x] CLI smoke tests via typer `CliRunner`
- [x] CI pipeline: pytest + ruff + mypy on every push (`.github/workflows/ci.yml`)

## Phase 12b -- Security Testing & Continuous Audit

Priority: **high** -- valider les corrections de la phase 10 et prevenir les regressions.

- [x] SAST (Static Application Security Testing)
  - [x] Bandit intégré dans `.github/workflows/security.yml` (medium+ bloquant)
  - [x] Config `[tool.bandit]` dans `pyproject.toml` avec exclusions documentées
  - [x] Semgrep avec règles custom HivePilot (`.semgrep/hivepilot.yml`) : injection shell, hardcoded secrets, CORS, utcnow()
- [x] Dependency audit
  - [x] `pip-audit` dans le CI security workflow
  - [x] Dependabot configuré (`.github/dependabot.yml`) — pip + GitHub Actions, weekly, groupé par domaine
- [x] Tests de sécurité dédiés (`tests/test_security.py`)
  - [x] Fuzzing `extra_prompt` (14 payloads : null bytes, unicode, XSS, SQLi, template injection…)
  - [x] Injection shell étendue (process substitution, here-string, arithmetic, glob, null byte…)
  - [x] Auth bypass API (no token, empty bearer, non-bearer scheme, invalid, expired, mauvais role, tokens malformés)
  - [x] Path traversal étendu (8 vecteurs : URL-encoded, null byte, symlink, dot-slash…)
  - [x] Volume mount container (sensitives paths bloqués, safe paths autorisés)
  - [x] Rate limiting (429 au-delà du seuil)
  - [x] Token timing safety (hmac.compare_digest)
- [x] Secret scanning
  - [x] Pre-commit hook `detect-secrets` + `gitleaks` (`.pre-commit-config.yaml`)
  - [x] Baseline `.secrets.baseline` initialisé
  - [x] Gitleaks dans le CI security workflow (scan historique complet)
- [ ] Pen test periodique
  - Checklist OWASP Top 10 appliquee aux endpoints FastAPI
  - Test des ChatOps endpoints avec payloads malicieux
  - Validation de l'isolation container runner (escape, mount sensible)

## Phase 13 -- Dependency Cleanup & Startup Performance

Priority: **high** -- `hivepilot --help` loads ML models and AWS SDK.

- [x] Restructure `pyproject.toml`: lightweight core + optional extras
  - Core: typer, rich, pydantic, PyYAML, gitpython, structlog, tenacity, questionary, python-dotenv, requests
  - `[langchain]`: langchain, langchain-community, faiss-cpu, nltk
  - `[crewai]`: crewai
  - `[langgraph]`: langgraph
  - `[dashboard]`: textual
  - `[api]`: fastapi, uvicorn, prometheus-client, itsdangerous
  - `[notifications]`: python-telegram-bot
  - `[cloud]`: boto3
  - `[containers]`: docker
  - `[full]`: all of the above
- [x] Lazy-load heavy imports — guard with try/except + message clair
  - `knowledge_service.py`: langchain/faiss/HuggingFace chargés dans `build_context()` et `_load_or_build()` uniquement
  - `artifact_service.py`: boto3 importé dans `_export_s3()` uniquement
  - `langchain_runner.py`: LangChain importé dans `run()` uniquement
  - `engines.py`: langgraph/crewai chargés via `importlib` à l'exécution uniquement
- [x] Defer `knowledge_service` init — répertoires créés à l'usage, pas à l'import
- [x] Defer `artifact_service` boto3 import to `_export_s3()`
- [x] Stop creating `runs/logs/` directory at import time (`utils/logging.py`)
  - File logging opt-in via `HIVEPILOT_LOG_TO_FILE=true` (défaut: false)
  - Répertoire créé uniquement si `log_to_file=True` et au premier log
- [x] Fix `ClaudeRunner.profiles` class-level mutable default — `field(default_factory=load_claude_profiles)`

## Phase 14 -- Architecture Refactoring

Priority: **medium** -- improve maintainability and testability.

- [x] Replace `Orchestrator.refresh()` calling `self.__init__()` with a `_load()` method
- [x] Remove module-level `Orchestrator()` in `api_service.py` and `chatops_service.py`
  - Use lazy singleton (double-checked locking with `threading.Lock`)
- [x] Make `POLICIES` reloadable — lazy `_cache` dict with `reload_policies()` + `monkeypatch.setitem` in tests
- [x] Refactor `chatops_service.py` -- extract common dispatch pattern for Slack/Discord/Telegram
- [x] Remove dead code: `PipelineExecutionContext` in `pipelines.py` and `utils/shell.py` (unused)
- [x] Standardize type hints (modern PEP 604/585 style everywhere)
- [x] Replace deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`
- [x] Fix `init_db()` called on every state_service operation -- `_ensure_db()` guard (once per process)
- [x] Improve notification_service error handling: distinguish "not configured" vs "configured but failing"
- [x] Validate `engines.py` module:function strings at config load time — `TaskConfig.validate_engine_refs` Pydantic validator
  - [x] Clear import error if LangGraph/CrewAI not installed (`pip install hivepilot[langgraph|crewai]`)
- [x] Detect missing `gh` CLI — `_require_gh()` guard in `github_service.py` (ensure_repository, create_issue, create_release)

---

## Phase 23b -- Telegram Bot (dual-mode)

Priority: **medium** -- contrôle à distance depuis n'importe quel environnement.

- [x] Settings : `telegram_bot_token`, `telegram_allowed_chat_ids`, `telegram_webhook_url`, `telegram_webhook_secret`, `telegram_webhook_port`
- [x] `telegram_bot.py` : handlers async pour `/run`, `/approvals`, `/approve`, `/deny`, `/status`, `/help`
- [x] Auth par whitelist `telegram_allowed_chat_ids` (ouvert si liste vide)
- [x] **Mode polling** (`run_polling()`) — RPI / NAT, zéro URL publique requise
- [x] **Mode webhook built-in** (`run_webhook()`) — VPS HTTPS, serveur intégré python-telegram-bot
- [x] **Mode webhook FastAPI** (`process_update()`) — partage le port API, endpoint `/webhook/telegram/{path}`
- [x] Vérification `X-Telegram-Bot-Api-Secret-Token` sur l'endpoint FastAPI
- [x] Graceful shutdown du bot lors de l'arrêt FastAPI
- [x] CLI : `hivepilot telegram start --mode polling|webhook`
- [x] CLI : `hivepilot telegram set-webhook <url>` / `delete-webhook` / `info`
- [x] Fallback sur `TELEGRAM_BOT_TOKEN` env var (compat notification_service existant)
- [x] Tests : whitelist, token, tous les handlers, cas limites

---

## Phase 14c -- Config Repo Sync

Priority: **medium** -- store orchestrator configs in a private GitHub repo for versioning and portability.

- [x] Add `config_repo` and `config_branch` settings (`HIVEPILOT_CONFIG_REPO`, `HIVEPILOT_CONFIG_BRANCH`)
- [x] `config_service.py`: `sync()` clones/pulls remote → copies managed files to `base_dir`
- [x] `config_service.py`: `push()` copies managed files from `base_dir` → commits + pushes to remote
- [x] `config_service.py`: `get_status()` and `get_log()` for visibility
- [x] CLI: `hivepilot config sync / push / status / log`
- [x] Fully optional — zero behaviour change when `HIVEPILOT_CONFIG_REPO` is not set
- [x] Managed files: `projects.yaml`, `tasks.yaml`, `pipelines.yaml`, `policies.yaml`, `schedules.yaml`, `prompts/`
- [x] Tests: sync copies files, push commits changes, no-op when clean, error when repo not configured

---

## Phase 14d -- Environment Portability & Proxy Support

Priority: **medium** -- HivePilot doit fonctionner sans modification sur RPI, VM, VPS, baremetal, derrière NAT, derrière un reverse proxy ou un proxy sortant.

### Proxy sortant (outbound)
- [x] Setting `HIVEPILOT_HTTP_PROXY` / `HIVEPILOT_HTTPS_PROXY` / `HIVEPILOT_NO_PROXY` (propagés aux sous-processus via `utils/env.proxy_env()`)
- [x] `requests` (notifications, chatops) : automatique via env vars — vérifié, aucun bypass
- [x] `python-telegram-bot` : proxy via env vars HTTP_PROXY/HTTPS_PROXY (httpx les lit automatiquement)
- [x] `gitpython` : proxy via `GIT_HTTP_PROXY` / `GIT_HTTPS_PROXY` — propagé dans `_git_env()` (git_service + config_service)

### Reverse proxy (inbound)
- [x] Rate limiter utilise l'IP réelle (`X-Forwarded-For`) et non l'IP du proxy
- [x] Support `root_path` FastAPI pour déploiement derrière nginx/caddy/traefik (`HIVEPILOT_API_ROOT_PATH`)
- [x] Respecter `X-Forwarded-Proto`, `X-Forwarded-Host` via `ProxyHeadersMiddleware` (uvicorn)

### Déploiement universel
- [x] `hivepilot doctor` étend son diagnostic : proxy, binaires externes, extras Python, config repo, Telegram
- [x] Systemd unit file généré par `hivepilot api systemd-unit` pour déploiement baremetal/VPS
- [x] `X-Request-ID` middleware — correlation ID propagé dans tous les logs et réponses API
- [x] Body size limit — `settings.api_max_body_size` (défaut 1 MB), retourne 413 si dépassé
- [x] Support `.env` par environnement : `.env.rpi`, `.env.vps`, `.env.dev` — chargé via `HIVEPILOT_ENV_FILE`
- [x] Support des bots en mode polling derrière NAT (23b) — zéro config réseau requise

## Phase 14e -- Caddy Auto-Setup (full automatisé)

Priority: **medium** -- HTTPS zéro-config pour exposer l'API et les webhooks sur VPS/baremetal.

### Génération du Caddyfile
- [x] `hivepilot caddy generate` — génère un `Caddyfile` complet depuis les settings HivePilot
  - `reverse_proxy localhost:{api_port}` automatique
  - Security headers (HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
  - Mode interne (self-signed) si `--tls-internal`
  - Email ACME optionnel pour Let's Encrypt
- [x] `hivepilot caddy show` — affiche le Caddyfile en production

### Installation & démarrage
- [x] `hivepilot caddy setup` — pipeline complet en une commande :
  1. Détecte si Caddy est installé (sinon installe via apt + cloudsmith repo)
  2. Génère et écrit le Caddyfile dans `/etc/caddy/Caddyfile`
  3. Valide la config avec `caddy validate`
  4. Active et démarre le service systemd `caddy`
- [x] `hivepilot caddy reload` — recharge la config Caddy sans coupure
- [x] `hivepilot caddy status` — état du service systemd Caddy
- [x] `hivepilot caddy logs` — tail des logs Caddy via journalctl

### Intégration avec les bots
- [x] `hivepilot caddy teardown` — stoppe et désactive Caddy
- [x] Après `hivepilot caddy setup`, enregistrer automatiquement les webhooks bots configurés
  - `hivepilot telegram set-webhook` appelé si `HIVEPILOT_TELEGRAM_BOT_TOKEN` présent

---

## Phase 14b -- API Maturity

Priority: **medium** -- fondations nécessaires avant toute exposition publique.

- [x] Versioning des endpoints sous `/v1/` (tous les endpoints disponibles sur `/` et `/v1/`)
- [x] Correlation ID (`X-Request-ID`) généré ou propagé, retourné dans les headers de réponse
- [x] Limite de taille max sur le body FastAPI (`settings.api_max_body_size`, défaut 1 MB, retourne 413)

---

## Phase 15 -- Scheduler Resilience & Retry Queue

- [x] Persistent retry queue with exponential backoff (`retry_jobs` SQLite table, `retry_service.py`)
  - Backoff formula: `base_delay * 2^(attempt-1)` — default 2m, 4m, 8m
- [x] Dead-letter queue — jobs exceeding `max_attempts` promoted to `dead` status
  - `hivepilot schedule dlq-list` / `dlq-purge` commands
- [x] Scheduler health checks — `hivepilot schedule health` shows due times, retry depth, DLQ count
- [x] Graceful shutdown — `SchedulerDaemon` handles SIGTERM/SIGINT, waits for in-flight tasks (configurable timeout)
- [x] `hivepilot schedule daemon` — long-running process replacing ad-hoc cron
- [x] `hivepilot schedule systemd-unit` — generates systemd unit file for daemon
- [x] `schedule_service.run_entry()` — integrates retry on failure; `cli schedule run` updated
- [x] Tests: enqueue, due jobs, success/retry/DLQ paths, purge, schedule integration (12 tests)

## Phase 16 -- Multi-Agent Collaboration Playbooks

- [ ] Agent-to-agent communication protocol
- [ ] Shared context and artifact passing between agents
- [ ] Playbook templates for common multi-agent patterns
- [ ] Conflict resolution when agents modify the same files

## Phase 17 -- Infrastructure-as-Code Integration

### 17a -- IaC Runners (declarative)

- [x] **OpenTofu** runner — plan / apply / destroy / output / validate / drift
- [x] **Terraform** runner — plan / apply / destroy / output / validate / drift
- [x] **Pulumi** runner — preview / up / destroy / output / refresh
- [x] Drift detection — `tofu/terraform plan --detailed-exitcode` → RuntimeError("Drift detected")
- [x] CLI: `hivepilot iac plan|apply|destroy|drift|output --project <name> --runner opentofu|terraform|pulumi`
- [x] Cost estimation before apply — `hivepilot iac cost --project <name>` (requires infracost CLI)
- [x] Remote backends (S3, GCS, Terraform Cloud) — `backend_config` option in runner definition

### 17b -- Configuration Management Runners

- [ ] **Ansible** runner — playbook execution, inventory dynamique, vault decrypt
- [ ] **Salt** runner — state apply, highstate, pillar refresh
- [ ] **Chef / Puppet** runner — cookbook apply, manifest apply (legacy support)

### 17c -- Specialized Tooling Runners

- [ ] **Packer** runner — build d'images VM/container (HCL templates)
- [ ] **Helm** runner — install / upgrade / rollback de charts Kubernetes
- [ ] **Kustomize** runner — build + apply de configurations Kubernetes
- [ ] **Atlantis** runner — déclencher des workflows Atlantis via API (plan/apply sur PR)

### 17d -- Linear Integration

- [ ] Créer automatiquement une issue Linear sur échec de run
- [ ] Mettre à jour le statut d'une issue Linear à la complétion d'un task
- [ ] Déclencher un run HivePilot depuis un webhook Linear (changement de statut, assignation)
- [ ] Lier un pipeline HivePilot à un cycle / projet Linear
- [ ] CLI : `hivepilot linear sync` — synchroniser les tâches Linear avec les tasks HivePilot

## Phase 18 -- Observability Hooks (OpenTelemetry)

- [ ] OpenTelemetry tracing for pipeline and step execution
- [ ] Span propagation across runners
- [ ] Export to Jaeger/Zipkin/OTLP
- [ ] Trace-aware error correlation

## Phase 19 -- Policy-Driven Secrets Backends (Vault/SOPS/KMS)

- [ ] HashiCorp Vault integration
- [ ] SOPS-encrypted file support
- [ ] AWS KMS / GCP KMS envelope encryption
- [ ] Secret rotation and TTL enforcement

## Phase 20 -- Drift Detection & Auto-Remediation Guardrails

- [ ] Baseline snapshots of repo/infra state
- [ ] Periodic drift detection scans
- [ ] Auto-remediation with approval gates
- [ ] Drift reports and alerting

## Phase 21 -- Supply Chain Scanning & SBOM Export

- [ ] Dependency vulnerability scanning (OSV, Grype)
- [ ] SBOM generation (CycloneDX / SPDX)
- [ ] License compliance checks
- [ ] Pipeline gate: block runs on critical CVEs

## Phase 22 -- Blueprinted Project Templates & Starters

- [x] Template registry for project scaffolding (minimal, blog, iac, security)
- [x] `hivepilot init` command with template selection + `--list`
- [x] Variable interpolation in templates (`{{project_name}}`, `{{project_path}}`, `{{author}}`)
- [x] Community template marketplace — `hivepilot templates list-remote [--source user/repo]` + `hivepilot templates pull <name>`

## Phase 23 -- Advanced ChatOps Actions

- [x] `diff` / `rollback` via chat commands (`/diff <project>`, `/rollback <project>`)
- [x] Run progress streaming to chat (heartbeat every 60s + ack message deleted on completion)
- [x] Contextual help and command discovery in chat (`/help` updated with all commands)
- [x] Output back to Telegram — run results sent as reply after completion
- [x] Singleton orchestrator reuse in bot handlers (via `chatops_service._get_orchestrator()`)
- [x] `auto_git=True` in bot `/run` handler — honours task git config (commit + push)
- [x] Free-form instructions via `/run <project> <task> <instructions>` passed as `extra_prompt`
- [x] Interactive approval threads — inline ✅ Approve / ❌ Deny buttons sent proactively
  - `notify_approval_required()` envoie le keyboard au `telegram_notification_chat_id`
  - `CallbackQueryHandler` gère les clics, édite le message avec le résultat
  - `/approvals` liste les runs en attente avec un keyboard par run
  - Setting `HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID` pour les notifications proactives

## Phase 23c -- Slack Bot (dual-mode)

Priority: **medium** -- parité avec le bot Telegram.

- [ ] Settings : `slack_bot_token`, `slack_signing_secret`, `slack_allowed_channel_ids`, `slack_socket_mode`, `slack_app_token`
- [ ] **Mode Socket Mode** (`slack_bolt` + `SocketModeHandler`) — RPI / NAT, zéro URL publique, WebSocket vers Slack
- [ ] **Mode HTTP webhook** — VPS, endpoint `/webhook/slack` dans FastAPI, vérification `X-Slack-Signature`
- [ ] Commandes slash : `/hp run <project> <task>`, `/hp approvals`, `/hp approve <id>`, `/hp deny <id>`, `/hp status`
- [ ] Notifications proactives (run terminé, approbation requise) vers un channel configuré
- [ ] Auth par channel ID whitelist (`slack_allowed_channel_ids`)
- [ ] CLI : `hivepilot slack start --mode socket|webhook`
- [ ] Graceful shutdown intégré au lifecycle FastAPI
- [ ] Tests : handlers, auth, cas limites

## Phase 23d -- Discord Bot (dual-mode)

Priority: **medium** -- parité avec le bot Telegram.

- [ ] Settings : `discord_bot_token`, `discord_allowed_guild_ids`, `discord_allowed_channel_ids`, `discord_public_key`
- [ ] **Mode Gateway** (`discord.py` / `py-cord`) — RPI / NAT, WebSocket persistant vers Discord, zéro URL publique
- [ ] **Mode HTTP interactions** — VPS, endpoint `/webhook/discord` dans FastAPI, vérification signature Ed25519
- [ ] Slash commands Discord : `/run`, `/approvals`, `/approve`, `/deny`, `/status`
- [ ] Notifications proactives vers un channel configuré
- [ ] Auth par guild ID + channel ID whitelist
- [ ] CLI : `hivepilot discord start --mode gateway|webhook`
- [ ] Graceful shutdown intégré au lifecycle FastAPI
- [ ] Tests : handlers, auth, vérification signature, cas limites

## Phase 24 -- Insight Dashboard & SLA Reporting

- [ ] Historical run analytics and trends
- [ ] SLA definition and tracking per project/task
- [ ] Cost tracking per LLM provider
- [ ] Exportable reports (PDF/CSV)

---

## Phase 25 -- Hugo Blog Publishing

> **Architecture note**: This is implemented as configuration, not engine code.
> The engine already has everything needed (Claude runner, shell runner, GitActions, approval flow, scheduler).
> See [github.com/jsoyer/hivepilot-config](https://github.com/jsoyer/hivepilot-config) for the full setup:
> tasks, prompts, validation script, and schedules.

The one engine addition that remains useful:

- [x] Generic named webhook trigger: `POST /webhook/trigger/{schedule_name}` — fires a named
  schedule entry on demand, usable from any external tool (Zapier, n8n, mobile shortcut).
  Protected by Bearer token. Returns immediately, executes async.
