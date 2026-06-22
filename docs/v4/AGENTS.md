# HivePilot V4 — The Agent Company

The `company` pipeline runs a full software org as a chain of role-bound agents.
Each stage is a task (`tasks.yaml`) bound to a **role**; the role decides which
**runner** (CLI tool) and **model** execute it (`roles.py`, overridable per
project via `policies.yaml`).

## Agent roster (`roles.py`) — canonical reference

| # | Agent | Role | Runner | Model(s) | Can block | Function |
|---|---|---|---|---|---|---|
| 1 | **Aliénor** | CEO | opencode | `qwen3.7-max` + `kimi-k2.6` ² | no | Debate (2 proposals + synthesis) → strategic direction |
| 2 | **Jules** | Chief of Staff (CSO) | cursor | (default) | no | Synthesizes CEO+CTO+CISO → proposal; final check + **approves the PR** |
| 3 | **Blaise** | CTO | opencode | `kimi-k2.7-code` | **yes** | Architecture |
| 4 | **Gustave** | Developer | claude | (default) | no | Implementation |
| 5 | **Victor** | Reviewer | codex | `gpt-5.5` | **yes** | Code review → **opens the PR** |
| 6 | **Hugo** | CISO | opencode | `glm-5.2` | **yes** | Security (architecture + code clearance) |
| 7 | **Marie** | QA | cursor | (default) | no | Tests / edge cases |
| 8 | **Théo** | Documentation | gemini | (default) | no | Docs + README per component repo |

² = **dual-model** (debate → synthesis). CEO only. `(default)` = no pinned model
(the CLI uses its own local config/login).

**Meta-agent (outside the pipeline): Henri** — external auditor, runs on Mistral
via the `vibe` runner; observes cycles and **proposes** prompt improvements
(`hivepilot audit <project> [--deep]`), never auto-applies. See USAGE.md.

> The 10-stage `company` pipeline below is the original. The reordered
> **`noxys-v2`** (planning → plan checkpoint → dev → … → Jules approves the PR)
> is documented in [USAGE.md](USAGE.md).

## The chain (pipeline `company`)

| # | Stage | Task | Role | Runner | Model | git |
|---|---|---|---|---|---|---|
| 1 | CEO Intake | noxys-ceo-intake | ceo | opencode | qwen3.7-max **+** kimi-k2.6 → **debate→ADR** | — |
| 2 | Chief of Staff Plan | noxys-cos-plan | chief_of_staff | cursor | (default) | — |
| 3 | CTO Review | noxys-cto-review | cto | opencode | kimi-k2.7-code | — |
| 4 | Implementation | noxys-developer | developer | claude | (default) | commit + push **branch** |
| 5 | Review | noxys-reviewer | reviewer | **codex** | (default) | **review → open PR** |
| 6 | Security | noxys-ciso | ciso | opencode | glm-5.2 | — |
| 7 | QA | noxys-qa | qa | **cursor** | (default) | — |
| 8 | Documentation | noxys-documentation | documentation | **gemini** | (default) | commit |
| 9 | Report | noxys-cos-report | chief_of_staff | cursor | (default) | — |
| 10 | Approval | noxys-ceo-approval | ceo | opencode | qwen3.7-max + kimi-k2.6 → **debate→ADR** | — |

So: **codex** does code review, **gemini** does documentation, **claude**
implements, **opencode** drives the strategy/security roles (qwen/kimi/glm), and
**cursor** drives QA + coordination (a dedicated QA runner, separate from docs).

## Code review & pull requests

- **Developer (claude)** implements, commits, and pushes the branch
  `hivepilot/<project>` — **no PR yet**.
- **Reviewer (codex)** reviews the pushed branch, then **opens the PR**
  (`git_service.create_pr` via `gh`) — so review happens **before** the PR exists.
- A **human merges** the PR. (Agents never merge.)
- This only happens when the run is invoked with `--auto-git` **and** the
  project policy has `allow_auto_git: true`. With `require_approval: true`
  (e.g. noxys) the run still waits for a human `/approve` before anything runs.

## Dual-model debates

One role is **bi-modal** — CEO (`qwen3.7-max` + `kimi-k2.6`). On such a stage
the orchestrator runs **each brain** (each via its own runner), captures its
proposal as a `Position`, synthesizes via `DebateService`, and writes an **ADR**
to the vault (`03 - Decisions/`). Brains are written `runner:model`.

CTO (Blaise) and CISO (Hugo) run **single-model opencode** — the claude brain was
removed to spare the claude quota the developer stage needs. They no longer trigger
a dual-model debate and do not produce debate ADRs. Also invokable directly:
`hivepilot debate <project> <topic>` (CEO only).

## Role → runner + model resolution

`roles.resolve_runner(role, policy)`:
1. default runner+model from `roles.py`,
2. apply `policies.yaml > projects.<name> > role_overrides` (e.g. force an EU model),
3. enforce `allowed_runners` (raises if the resolved runner isn't whitelisted).

`roles.py` is the global default ("the company"); `policies.yaml` overrides per
project — so the same company runs on any repo, with per-project model/runner
tuning. See [CONFIG.md](CONFIG.md).

## Prompts & governance

Each role's behaviour is a prompt in `prompts/agents/<role>.md`. The prompts
reference the target project's governance files **by path** (read, not copied):
`CLAUDE.md`, `AGENTS.md`, `AGENT-GOVERNANCE.md`, `.cursorrules`, `.windsurfrules`,
`GEMINI.md`, and the vault's `AGENT-DETECTION-FABRIC.md`.
