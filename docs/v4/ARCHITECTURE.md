# HivePilot V4 — Architecture

HivePilot orchestrates a **company of AI agents** that run real CLI coding tools
(claude, opencode, gemini, codex, cursor) against a target repository, with full
visibility, approval gates, and an Obsidian "second brain".

HivePilot is a **standalone tool**. A "project" is just a target repo you point it
at; the agent company, pipeline, roles and runners are project-agnostic config.

```
 CLI  /  Telegram  /  HTTP API
        │
        ▼
   Orchestrator ──────────────► RunnerRegistry ──► runners (claude/opencode/gemini/codex/cursor)
        │                                              │ (real agent CLIs, non-interactive)
        │                                              ▼
        ├─ state_service (SQLite): runs, steps, interactions, audit, approvals, retry_queue, tokens
        ├─ policy_service: per-project policy + role/model overrides
        ├─ roles.py: role → runner + model
        └─ ObsidianService / InteractionService / DebateService ──► Obsidian vault
                                                                     (notes, Mermaid timeline, ADRs)
```

## Core components

| Component | File | Responsibility |
|---|---|---|
| Orchestrator | `hivepilot/orchestrator.py` | Runs tasks & pipelines; per-stage role→runner resolution; debate; state transitions; per-stage interaction logging |
| Roles | `hivepilot/roles.py` | The role registry + `resolve_runner(role, policy)` → effective (runner kind, model) |
| Runner registry | `hivepilot/registry.py` | Maps a runner *kind* → runner class; `execute`/`execute_definition`/`capture_definition` |
| Runners | `hivepilot/runners/*` | Wrap each agent CLI (non-interactive); `claude`, `opencode`/`gemini`/`codex`/`ollama` (PromptCliRunner), `cursor`, plus `shell`/`container`/`internal`/`langchain` |
| State | `hivepilot/services/state_service.py` | SQLite: runs, steps, **interactions**, **audit_log**, approvals, **retry_queue**, tokens |
| Policy | `hivepilot/services/policy_service.py` | Per-project policy (approval/auto-git/containers) + **role_overrides** + **allowed_runners** |
| Vault | `hivepilot/services/obsidian_service.py` | Safe writes under the Obsidian vault (dry-run aware) |
| Interactions | `hivepilot/services/interaction_service.py` | Dual-write each interaction: SQLite row + Obsidian note; Mermaid timeline |
| Debate | `hivepilot/services/debate_service.py` | Synthesize role positions → ADR |
| Knowledge | `hivepilot/services/knowledge_service.py` | Prompt context from `knowledge_files` (plain read by default; optional embedding RAG) |
| Surfaces | `cli.py`, `services/telegram_bot.py`, `ui/dashboard.py`, `services/api_service.py` | CLI, Telegram, TUI dashboard, HTTP API |

## Execution flow (a pipeline run)

1. `run_pipeline(project, pipeline, dry_run, simulate)` opens a run (`RunStatus.RUNNING`).
2. For each **stage** (= a task in `pipelines.yaml`):
   - `run_task` → `_execute_task` for the task's `role`.
   - `resolve_runner(role, policy)` → effective runner kind + model (per-project overridable).
   - If the role has **multiple models** (CEO) → `run_debate` (each model's output → positions → **ADR**).
   - Otherwise the runner executes the step's `prompt_file` with the resolved runner+model.
   - A per-stage **Interaction** is logged (store + Obsidian note); a per-stage artifact is written.
3. Status transitions persist; fail-fast unless `continue_on_failure`.

## Safety model

- **dry-run default** for all vault writes (notes, ADRs, artifacts).
- **`--simulate`** records steps as success **without invoking any agent CLI** (and bypasses approval) — for validating wiring.
- **Approval gate**: a project with `require_approval: true` queues runs for human `/approve` before execution.
- **Input hygiene**: `utils/validation.py` sanitizes prompts + flags injection patterns (non-blocking); container runner blocks sensitive volume mounts.
- **Lightweight core**: langchain/torch/boto3 are optional extras (lazy imports) — the CLI starts without them.

See also: [AGENTS.md](AGENTS.md) (the company chain), [USAGE.md](USAGE.md), [CONFIG.md](CONFIG.md), [NOXYS.md](NOXYS.md).
