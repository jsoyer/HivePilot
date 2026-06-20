# HivePilot V4 — The Agent Company

The `company` pipeline runs a full software org as a chain of role-bound agents.
Each stage is a task (`tasks.yaml`) bound to a **role**; the role decides which
**runner** (CLI tool) and **model** execute it (`roles.py`, overridable per
project via `policies.yaml`).

## The chain (pipeline `company`)

| # | Stage | Task | Role | Runner | Model | git |
|---|---|---|---|---|---|---|
| 1 | CEO Intake | company-ceo-intake | ceo | opencode | qwen3.7-max **+** kimi-k2.6 → **debate→ADR** | — |
| 2 | Chief of Staff Plan | company-cos-plan | chief_of_staff | cursor | (default) | — |
| 3 | CTO Review | company-cto-review | cto | opencode | kimi-k2.7-code | — |
| 4 | Implementation | company-developer | developer | claude | (default) | commit + push **branch** |
| 5 | Review | company-reviewer | reviewer | **codex** | (default) | **review → open PR** |
| 6 | Security | company-ciso | ciso | opencode | glm-5.2 | — |
| 7 | QA | company-qa | qa | gemini | (default) | — |
| 8 | Documentation | company-documentation | documentation | **gemini** | (default) | commit |
| 9 | Report | company-cos-report | chief_of_staff | cursor | (default) | — |
| 10 | Approval | company-ceo-approval | ceo | opencode | qwen3.7-max + kimi-k2.6 → **debate→ADR** | — |

So: **codex** does code review, **gemini** does QA + documentation, **claude**
implements, **opencode** drives the strategy/security roles (qwen/kimi/glm), and
**cursor** drives coordination.

## Code review & pull requests

- **Developer (claude)** implements, commits, and pushes the branch
  `hivepilot/<project>` — **no PR yet**.
- **Reviewer (codex)** reviews the pushed branch, then **opens the PR**
  (`git_service.create_pr` via `gh`) — so review happens **before** the PR exists.
- A **human merges** the PR. (Agents never merge.)
- This only happens when the run is invoked with `--auto-git` **and** the
  project policy has `allow_auto_git: true`. With `require_approval: true`
  (e.g. noxys) the run still waits for a human `/approve` before anything runs.

## CEO dual-model debate

The CEO role has two models (`qwen3.7-max`, `kimi-k2.6`). On a CEO stage the
orchestrator runs **each model**, captures its proposal as a `Position`,
synthesizes via `DebateService`, and writes an **ADR** to the vault
(`03 - Decisions/`). Also invokable directly: `hivepilot debate <project> <topic>`.

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
