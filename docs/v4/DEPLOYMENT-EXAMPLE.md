# Example Deployment: Acme

HivePilot is standalone; **Acme** here is an example configured target, showing how
you'd set up and drive HivePilot against one of your own repos.

## What's configured

- **Project** `acme` → `/home/you/Documents/Github/acme` (`projects.yaml`).
- **Vault**: `settings.obsidian_vault` points at `…/obsidian-vault/Acme`
  (interaction notes, Mermaid timelines, and debate ADRs land there).
- **Policy** (`policies.yaml > projects.acme`):
  - `require_approval: true` — every run waits for a human `/approve`.
  - `allow_auto_git: true` — the developer stage may push a branch + open a PR (you merge).
  - `allow_containers: false`.
- **Models** = an example mapping with real opencode IDs — see the table below.
- **Governance**: the role prompts read the target repo's `CLAUDE.md`, `AGENTS.md`,
  `AGENT-GOVERNANCE.md`, `.cursorrules`, `.windsurfrules`, `GEMINI.md`, and the vault
  `AGENT-DETECTION-FABRIC.md` — when present.

> Model sovereignty note: if a target repo's `AGENTS.md` mandates a specific model
> region/vendor policy (e.g. EU-sovereign-only) but the configured models
> (qwen/kimi/glm) don't meet it, that's an explicit tradeoff to document (perf vs.
> sovereignty). To switch later, add `role_overrides` / `allowed_runners` for
> `acme` in `policies.yaml` — no code change.

## Effective role → runner + model (Acme example)

This example uses the global `roles.py` defaults (no per-project `role_overrides`),
so the effective mapping is:

| Role | Runner | Model | Pipeline stage(s) |
|---|---|---|---|
| ceo | opencode | `opencode-go/qwen3.7-max` + `opencode-go/kimi-k2.6` (→ debate→ADR) | CEO Intake, CEO Approval |
| chief_of_staff | cursor | (cursor default) | Plan, Report |
| cto | opencode | `opencode-go/kimi-k2.7-code` | CTO Review |
| developer | claude | (claude default) | Implementation (commit + push branch) |
| reviewer | codex | (codex default) | Review (→ opens the PR) |
| ciso | opencode | `opencode-go/glm-5.2` | Security |
| qa | cursor | (cursor default) | QA — dedicated runner, distinct from docs |
| documentation | gemini | (gemini default) | Documentation |

> cursor is shared by `chief_of_staff` and `qa` (different roles, different stages).
> To customize this example without touching the global defaults, add
> `policies.yaml > projects.acme > role_overrides` / `allowed_runners`.

## Prerequisites for a real run

The agent CLIs must be authenticated for their models:
- **opencode** authenticated for `opencode-go/*` (qwen/kimi/glm),
- **claude**, **codex**, **cursor-agent**, **gemini** logged in.

Check: `hivepilot doctor` (lists the runner CLIs found on PATH).

## Driving this example project

```bash
# 1. Validate the full wiring — no agent calls, no approval needed:
hivepilot run-pipeline acme company --simulate

# 2. Real run — queues for approval (allow_auto_git lets the dev stage open a PR):
hivepilot run-pipeline acme company --auto-git
hivepilot approvals                 # review the pending run
# approve (CLI or Telegram /approve <id>) -> the company executes:
#   CEO debate -> CoS plan -> CTO -> Developer (commits + pushes branch) ->
#   Reviewer (codex reviews, then opens the PR) -> CISO -> QA -> Documentation
#   -> Report -> CEO approval
# You merge the PR.

# 3. A standalone CEO decision:
hivepilot debate acme "Adopt event-sourcing for the audit log?"
```

From Telegram (after enabling — see [USAGE.md](USAGE.md)): `/runpipeline acme company`,
`/interactions`, `/steps <run_id>`, `/approve <id>`.

## Relaxing / tightening later

Edit `policies.yaml > projects.acme`:
- drop `require_approval` to `false` once you trust the runs,
- set `allowed_runners` / `role_overrides` to enforce your preferred model policy,
- keep `allow_auto_git` on only if you want agents pushing branches.
