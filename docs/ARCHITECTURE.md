# Architecture

HivePilot is a config-driven orchestrator: a CLI drives an `Orchestrator` that resolves roles to runners to models, executes pipeline stages against one or more repos, and records everything it does to a local state store. Behavior — which agent runs a stage, which model it uses, what it's allowed to touch — is defined in YAML, not in code, so operational changes (swap a model, add a role, gate a stage behind approval) are config edits, not deploys.

## Component overview

```
                         ┌───────────────────────────┐
                         │   hivepilot (Typer CLI)   │
                         │   24 command groups        │
                         └─────────────┬─────────────┘
                                        │
                                        ▼
                         ┌───────────────────────────┐
                         │        Orchestrator        │
                         │  stage loop · dispatch      │
                         │  resolution · worktrees     │
                         │  approval gates · debate/    │
                         │  lessons hooks               │
                         └───┬───────────┬─────────┬───┘
                              │           │         │
              ┌───────────────┘           │         └───────────────┐
              ▼                           ▼                         ▼
    ┌───────────────────┐      ┌───────────────────┐      ┌──────────────────────┐
    │   Config layer     │      │   Runner layer      │      │      Services         │
    │ projects/tasks/     │      │ RunnerRegistry       │      │ state_service          │
    │ roles/pipelines/    │      │ dispatch by `kind`   │      │ debate_service         │
    │ policies/groups/    │      │                       │      │ lessons_service        │
    │ schedules/          │      │ claude / codex /      │      │ drift_service          │
    │ model_profiles      │      │ vibe / openrouter      │      │ analytics_service      │
    │ + pydantic Settings │      │ (built-in)             │      │ secrets                │
    │ (HIVEPILOT_ env)    │      │ + plugin kinds         │      │ notification_service   │
    └─────────────────────┘      │ (PATH-gated)           │      │ scheduler_daemon       │
                                  └──────────┬────────────┘      │ api_service (FastAPI)  │
                                              │                    │ interaction_service    │
                                              ▼                    └───────────┬───────────┘
                                  ┌───────────────────────┐                    │
                                  │  agent CLIs / APIs      │                    ▼
                                  │  (per runner, CLI or    │        ┌──────────────────────┐
                                  │   API mode)              │        │      State store       │
                                  └───────────────────────┘        │ SQLite state.db         │
                                                                     │ (runs/steps/interactions │
                                                                     │  /verdicts/lessons/drift │
                                                                     │  scans/tenants)          │
                                                                     │ runs/<ts>/summary.json   │
                                                                     │ structured JSON logs     │
                                                                     └──────────────────────────┘

    ┌───────────────────┐        feeds registry/services        ┌──────────────────────┐
    │      Plugins        │ ─────────────────────────────────▶ │ Dashboard / API /      │
    │ runners·notifiers·  │                                      │ Telegram (control       │
    │ hooks·secrets·       │ ◀───────────────────────────────── │ surfaces)                │
    │ panels·skills        │                                      └──────────────────────────┘
    └───────────────────┘
```

| Component | Description |
|---|---|
| CLI (`hivepilot`, Typer) | Entrypoint `hivepilot.cli:app`. 24 command groups (task, pipeline, config, role, project, drift, plugins, playbooks, and more) cover config editing, execution, and observability. |
| Orchestrator (`hivepilot/orchestrator.py`) | Runs tasks and pipelines. Resolves per-stage dispatch (runner/model/effort), manages git worktree isolation for git-mutating work, enforces approval gates, and invokes debate and lessons hooks around execution. |
| Runner layer | `RunnerRegistry` dispatches by `kind` to a `BaseRunner` subclass. Built-in kinds: `claude`, `codex`, `vibe`, `openrouter`. Plugin-contributed kinds are PATH-gated (only usable if the underlying binary is present). Each runner exposes a CLI invocation path; API-capable runners also expose an API path. |
| Config layer | YAML files for projects, tasks, roles, pipelines, policies, groups, schedules, and model_profiles, plus a pydantic `Settings` object read from `HIVEPILOT_`-prefixed environment variables. Each config file resolves in order: `$XDG_CONFIG_HOME/hivepilot/<file>` → config-repo → `base_dir`. |
| Services (`hivepilot/services/*`) | `state_service` (SQLite persistence), `debate_service`, `lessons_service`, `drift_service`, `analytics_service`, `secrets`, `notification_service`, `scheduler_daemon`, `api_service` (FastAPI), `interaction_service`, and others. |
| State store | SQLite `state.db` holding runs, steps, interactions, verdicts, lessons, drift scans, and tenants; per-run `runs/<timestamp>/summary.json`; structured JSON logs. |
| Plugin system | Six contribution types — runners, notifiers, hooks, secrets, panels, skills — loaded from Python entry-points or local `plugins/*.py`. |

## Execution flow

1. The CLI parses the command and loads config (projects/tasks/roles/pipelines/policies/…) via the resolution order above.
2. The `Orchestrator` resolves the pipeline's stages in order.
3. For each stage, it resolves the bound role's dispatch — runner, model, and reasoning effort — via the precedence chain: `policy > stage > role > runner-default`.
4. Approval gates are checked; a gated step pauses the run until approved (or auto-approved per policy).
5. For tasks that mutate a git repo, the stage executes inside an isolated git worktree.
6. The resolved runner is invoked, either in CLI mode or, for API-capable runners, in API mode.
7. Interactions and the step outcome are recorded to the state store.
8. If debate is enabled for the pipeline/stage, adjudication runs: a judge and, on disagreement, a challenge arbiter produce a verdict that can gate the PR.
9. If lessons are enabled, distillation runs at the end of the run to extract validated lessons for future runs.
10. Notifications fire through configured notifier plugins (e.g. Telegram, Discord, Slack, Obsidian).

## Role → runner → model resolution

Dispatch resolution follows one precedence chain, highest wins: `policy.role_overrides > stage > role > runner-default`.

- A role, defined in `roles.yaml`, binds a runner and a model (and optionally a reasoning effort).
- A pipeline stage may override runner/model/effort for that stage only.
- `policy` is the top-level security control — it defines `allowed_runners` (a gate) and `role_overrides` — and is applied **last**, so a stage or role definition cannot bypass a policy-level restriction.
- Reasoning `effort` is a single closed enum: `low | medium | high | xhigh | max`. It maps to runner-specific knobs (e.g. Claude's `MAX_THINKING_TOKENS`, a Codex effort flag).

See [PIPELINES-AND-ROLES.md](./PIPELINES-AND-ROLES.md) and [RUNNERS.md](./RUNNERS.md) for the full schema and built-in/plugin runner list.

## Safety model

- **Dry-run / simulate** — pipelines and tasks can be previewed without executing runners.
- **Approval gates** — a 3-tier gate model, plus automatic gating of destructive operations. The destructive-op check is fail-closed: if the check itself raises, the step is treated as destructive and gated.
- **Prompt-injection validation** — inputs assembled into agent prompts are validated before dispatch.
- **Secrets masking** — secret values are masked at every output sink (CLI, API, DB, notifications), not only at the point of use.
- **Fail-closed debate and lessons gates** — if a debate verdict or lesson-validation step errors or is inconclusive, the gate denies rather than defaulting to allow.
- **Optional container isolation** — execution can be sandboxed in a container for additional isolation.
- **Lightweight core** — heavy dependencies (`langchain`, `torch`, `boto3`, etc.) are optional extras, not core requirements.

See [SECURITY.md](./SECURITY.md) for the full threat model and gate configuration.

## State & observability

Every run is persisted to the SQLite state store (runs, steps, interactions, verdicts, lessons, drift scans, tenants), mirrored to a per-run `runs/<timestamp>/summary.json`, and logged as structured JSON. A read-only analytics API exposes run history, durations (p50/p95/p99), provider/cost breakdowns, and CSV export, tenant-scoped. OpenTelemetry tracing is opt-in via `HIVEPILOT_ENABLE_TRACING`: `pipeline.run`/`task.run`/`step.run` spans are exported via OTLP, a W3C `TRACEPARENT` header is propagated into every runner subprocess's environment (local execution; the SSH/remote path forwards it too, folded into the same env-assignment mechanism it already uses for other vars) so a downstream OTel-instrumented tool nests under the invoking `step.run` span, and structured log lines are enriched with `trace_id`/`span_id` when a span is recording, for log↔trace correlation in Jaeger/Grafana. All of this is a pure no-op when tracing is disabled.

See [DASHBOARD.md](./DASHBOARD.md) for the Mirador dashboard (TUI and web) and [DEPLOYMENT.md](./DEPLOYMENT.md) for running the API/dashboard in production.

## Extensibility

The plugin system is the primary extension surface: new runners, notifiers, lifecycle hooks, secrets backends, dashboard panels, and skills all load the same way, from entry-points or local `plugins/*.py`, under a fail-closed trust model. See [PLUGINS.md](./PLUGINS.md).

Config itself is extensible via GitOps: `hivepilot config sync` and `hivepilot config push` synchronize the YAML config tree with a separate config repo, so role/policy/pipeline changes go through the same review process as code.

## See also

- [PIPELINES-AND-ROLES.md](./PIPELINES-AND-ROLES.md) — pipeline/stage/role schema and precedence rules
- [RUNNERS.md](./RUNNERS.md) — built-in and plugin runner reference
- [PLUGINS.md](./PLUGINS.md) — plugin contribution types and loading
- [SECURITY.md](./SECURITY.md) — threat model, approval gates, secrets handling
- [DASHBOARD.md](./DASHBOARD.md) — Mirador TUI/web dashboard
- [DEPLOYMENT.md](./DEPLOYMENT.md) — running the API and scheduler in production
