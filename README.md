# 🐝 HivePilot

HivePilot is a YAML-driven orchestrator that runs a company of role-bound AI agents through a software-delivery pipeline against one or many repositories. It dispatches coding-agent CLIs (Claude Code, Codex, Vibe, plus OpenRouter and a range of PATH-gated agent plugins) and shell/LangChain runners, in CLI or API mode, from a single config-driven engine — with code review and PRs, an optional adjudicated debate that gates PR promotion, an opt-in auto-learning loop, remote control, and a plugin system.

## What it does

- Runs a configurable pipeline of role-bound agents (e.g. CEO → CTO → Developer → Reviewer → CISO → QA → Documentation) against a repo; each role resolves to a runner + model, overridable per project.
- Multi-runner: built-in agent kinds `claude`, `codex`, `vibe`, `openrouter` (API-only); PATH-gated plugin agents (gemini, opencode, ollama, pi, qwen-code, kimi-cli, antigravity); plus shell/LangChain/LangGraph/CrewAI engines. Each CLI runner can flip to API mode per YAML.
- Code review + Git/GitHub automation: branch/commit/push, PR create/draft/promote/merge, `gh` issue/release.
- Opt-in adjudicated debate: dual-model positions produce an ADR, with an optional independent LLM judge + challenge arbiter that fail-closed gates PR promotion (blocks `promote_pr`/`merge_pr` on any absent, low-confidence, or non-approval verdict).
- Opt-in auto-learning lessons loop: distills a run's verdicts/outcomes into candidate lessons, validates each against the run's real outcome (never an LLM self-report), and injects only validated lessons into future runs.
- Mirador dashboard: a TUI and a web command center (approve/deny, launch async runs, stop/cancel, toggle plugins), reading the SQLite state store tenant-scoped.
- Remote control via Telegram bot, Slack/Discord, and an HTTP API (`hivepilot api serve`).
- Plugin system: contribute runners, notifiers, lifecycle hooks, secrets backends, dashboard panels, and skills; loaded from installed packages or local files, fail-closed trust (no network fetch of plugin code).
- Infrastructure runners (terraform / opentofu / pulumi, kubectl) with destructive-op auto-gating, plus drift detection with scheduled scans and gated auto-remediation.

## Quickstart

Install the package:

```bash
pip install hivepilot
```

Optional extras:

```bash
pip install "hivepilot[full]"          # langchain + torch
pip install "hivepilot[notifications]" # Telegram
pip install "hivepilot[langchain]"     # langchain only
```

Check your environment and available agent binaries:

```bash
hivepilot doctor
```

Scaffold a workspace:

```bash
hivepilot init
```

Preview a pipeline before running anything for real (no agent calls are made):

```bash
hivepilot run-pipeline <project> <pipeline> --simulate
```

`run-pipeline` also defaults to `--dry-run`, so a plain run is safe to try without `--simulate`. For a guided, menu-driven session instead of raw commands:

```bash
hivepilot interactive
```

## Documentation

| Doc | Purpose |
| --- | --- |
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | Install, `doctor`, first pipeline, approval walkthrough |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the orchestrator, config, runners, and state fit together |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | The YAML config files and environment variables |
| [docs/CLI-REFERENCE.md](docs/CLI-REFERENCE.md) | Every command |
| [docs/PIPELINES-AND-ROLES.md](docs/PIPELINES-AND-ROLES.md) | The agent "company", roles, pipeline stages, groups/multi-repo |
| [docs/RUNNERS.md](docs/RUNNERS.md) | Agent runners (Claude/Codex/…), CLI vs API mode, IaC (terraform/pulumi) and kubectl runners |
| [docs/PLUGINS.md](docs/PLUGINS.md) | The plugin system (runners/notifiers/hooks/secrets/panels/skills) |
| [docs/SKILLS.md](docs/SKILLS.md) | Plugin-contributed skills |
| [docs/DEBATE-AND-LESSONS.md](docs/DEBATE-AND-LESSONS.md) | Dual-model debate + judge/arbiter PR gate, and the auto-learning lessons loop |
| [docs/SECURITY.md](docs/SECURITY.md) | Approval gates, secrets masking, CVE gate, fail-closed model |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | Telegram/Slack/Discord/Notion/Linear/Obsidian/Caddy/n8n/SSH remote agents |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Mirador dashboard (TUI + web) |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production deployment, Kubernetes, multi-tenant, observability |

## Safety

- `--simulate` previews a pipeline with no real agent calls; `run-pipeline` also defaults to `--dry-run`.
- Three-tier approval gates: policy-level, stage-level (`pause_before`), and step-level (`require_approval`), plus automatic gating of destructive operations.
- Prompt-injection validation on agent inputs.
- Secrets are masked in every sink (logs, notifications, state store); `${secret:NAME}` references resolve lazily and fail closed by default.
- The debate judge/arbiter PR gate is opt-in and fail-closed: it blocks PR promotion on any absent, low-confidence, or non-approval verdict.
- The core install stays lightweight — langchain, torch, and boto3 are optional extras, not defaults.

See [docs/SECURITY.md](docs/SECURITY.md) for details.

## Status

HivePilot is v0.2.0, requires Python >=3.10. See LICENSE.
