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

---

## Phase 10 -- Security Hardening (critical)

Priority: **immediate** -- blocks any production or shared deployment.

- [ ] Fix command injection in shell runner (`templates.py` uses raw `format_map`)
  - Escape all interpolated values with `shlex.quote()` before shell execution
  - Validate volume mount paths in container runner
- [ ] Fix CORS policy (`allow_origins=["*"]` + `allow_credentials=True`)
  - Restrict to configured origins or remove credentials
- [ ] Authenticate ChatOps endpoints (missing `require_role` on `/chatops/*`)
- [ ] Hash API tokens at rest (SHA-256) and compare with `hmac.compare_digest()`
  - Mask tokens in `hivepilot tokens list` output
- [ ] Move Google API key from URL query string to request header
- [ ] Add path validation to file-based secret resolver (whitelist allowed dirs)
- [ ] Add `state.db` and `api_tokens.yaml` to `.gitignore`
- [ ] Add rate limiting on `/run` and ChatOps endpoints

## Phase 11 -- Runtime Stability (critical)

Priority: **immediate** -- several code paths crash at runtime.

- [ ] Fix missing imports causing `NameError` at runtime
  - `knowledge_service.py`: `datetime`
  - `api_service.py`: `Dict`, `Any`, `Response`
  - `prompt_cli_runner.py`: `Any`
  - `chatops_service.py`: `settings.chatops_token` field missing from `Settings`
- [ ] Wire `timeout_seconds` from task step config into `subprocess.run()` calls
- [ ] Fix `run_approved` -- wrap `_execute_task` in try/except, set status to "failed" on error
- [ ] Fix `git_service.py` -- clean repo should warn/no-op, not raise `RuntimeError`
- [ ] Add pipeline fail-fast: stop on stage failure unless `continue_on_failure` is set

## Phase 12 -- Test Suite Foundation

Priority: **high** -- no tests exist today.

- [ ] Create `tests/` directory with `conftest.py` and pytest config
- [ ] Security-critical path tests first:
  - Shell runner template rendering (injection prevention)
  - Token service (hashing, comparison, CRUD)
  - Secret resolver (env, file, path validation)
  - Policy service (allow/deny logic)
- [ ] Runner tests (claude, shell, container, prompt-cli)
- [ ] Orchestrator tests (pipeline execution, concurrency, approval flow)
- [ ] CLI smoke tests via typer `CliRunner`
- [ ] CI pipeline: pytest + ruff + mypy on every push

## Phase 12b -- Security Testing & Continuous Audit

Priority: **high** -- valider les corrections de la phase 10 et prevenir les regressions.

- [ ] SAST (Static Application Security Testing)
  - Integrer Bandit dans le CI pour detecter les patterns dangereux (subprocess, eval, exec, format sur shell)
  - Configurer Semgrep avec des regles custom pour les injections shell et les secrets en dur
- [ ] Dependency audit
  - `pip-audit` ou `safety` dans le CI pour scanner les CVE sur les deps
  - Alertes automatiques sur les nouvelles vulnerabilites (Dependabot / Renovate)
- [ ] Tests de securite dedies
  - Fuzzing des inputs utilisateur (`extra_prompt`, noms de projets, chemins)
  - Tests d'injection sur tous les runners (shell, container, prompt-cli)
  - Tests de bypass d'authentification sur l'API (CORS, tokens invalides, roles insuffisants)
  - Tests de path traversal sur le secret file resolver
- [ ] Secret scanning
  - Pre-commit hook avec `detect-secrets` ou `gitleaks` pour bloquer les commits de tokens/cles
  - Scan retroactif de l'historique git
- [ ] Pen test periodique
  - Checklist OWASP Top 10 appliquee aux endpoints FastAPI
  - Test des ChatOps endpoints avec payloads malicieux
  - Validation de l'isolation container runner (escape, mount sensible)

## Phase 13 -- Dependency Cleanup & Startup Performance

Priority: **high** -- `hivepilot --help` loads ML models and AWS SDK.

- [ ] Restructure `pyproject.toml`: lightweight core + optional extras
  - Core: typer, rich, pydantic, PyYAML, gitpython, structlog, tenacity, questionary, python-dotenv
  - `[langchain]`: langchain, langchain-community, faiss-cpu, nltk
  - `[crewai]`: crewai
  - `[dashboard]`: textual
  - `[api]`: fastapi, uvicorn, prometheus-client, itsdangerous
  - `[notifications]`: python-telegram-bot, requests
  - `[cloud]`: boto3
  - `[containers]`: docker
  - `[full]`: all of the above
- [ ] Lazy-load heavy imports (langchain, boto3, faiss, docker, telegram)
  - Guard with try/except + clear error message when extra not installed
- [ ] Defer `knowledge_service` init until first use (not at import time)
- [ ] Defer `artifact_service` boto3 import to `_export_s3()`
- [ ] Stop creating `runs/logs/` directory at import time (`utils/logging.py`)
- [ ] Fix `ClaudeRunner.profiles` class-level mutable default -- use `field(default_factory=...)`

## Phase 14 -- Architecture Refactoring

Priority: **medium** -- improve maintainability and testability.

- [ ] Replace `Orchestrator.refresh()` calling `self.__init__()` with a `_load()` method
- [ ] Remove module-level `Orchestrator()` in `api_service.py` and `chatops_service.py`
  - Use FastAPI dependency injection or lazy singleton
- [ ] Make `POLICIES` reloadable (not a frozen module-level constant)
- [ ] Refactor `chatops_service.py` -- extract common dispatch pattern for Slack/Discord/Telegram
- [ ] Remove dead code: `PipelineExecutionContext` in `pipelines.py`
- [ ] Standardize type hints (modern PEP 604/585 style everywhere)
- [ ] Replace deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`
- [ ] Fix `init_db()` called on every state_service operation -- call once at startup
- [ ] Improve notification_service error handling: distinguish "not configured" vs "configured but failing"

---

## Phase 15 -- Scheduler Resilience & Retry Queue

- [ ] Persistent retry queue with exponential backoff
- [ ] Dead-letter queue for permanently failed tasks
- [ ] Scheduler health checks and self-healing
- [ ] Graceful shutdown with in-flight task completion

## Phase 16 -- Multi-Agent Collaboration Playbooks

- [ ] Agent-to-agent communication protocol
- [ ] Shared context and artifact passing between agents
- [ ] Playbook templates for common multi-agent patterns
- [ ] Conflict resolution when agents modify the same files

## Phase 17 -- Infrastructure-as-Code Integration (Terraform/Pulumi)

- [ ] Terraform runner for plan/apply workflows
- [ ] Pulumi runner with stack management
- [ ] Drift detection between declared and actual state
- [ ] Cost estimation before apply

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

- [ ] Template registry for project scaffolding
- [ ] `hivepilot init` command with template selection
- [ ] Variable interpolation in templates
- [ ] Community template marketplace

## Phase 23 -- Advanced ChatOps Actions

- [ ] `diff` / `apply` / `rollback` via chat commands
- [ ] Interactive approval threads
- [ ] Run progress streaming to chat
- [ ] Contextual help and command discovery in chat

## Phase 24 -- Insight Dashboard & SLA Reporting

- [ ] Historical run analytics and trends
- [ ] SLA definition and tracking per project/task
- [ ] Cost tracking per LLM provider
- [ ] Exportable reports (PDF/CSV)
