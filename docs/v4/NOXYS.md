# HivePilot for Noxys

HivePilot is standalone; **Noxys** is a configured target. This is its setup and
how to drive it.

## What's configured

- **Project** `noxys` → `/home/jeromesoyer/Documents/Github/noxys` (`projects.yaml`).
- **Vault**: `settings.obsidian_vault` already points at `…/obsidian-vault/Noxys`
  (interaction notes, Mermaid timelines, and debate ADRs land there).
- **Policy** (`policies.yaml > projects.noxys`):
  - `require_approval: true` — every run waits for a human `/approve`.
  - `allow_auto_git: true` — the developer stage may push a branch + open a PR (you merge).
  - `allow_containers: false`.
- **Models** = the approved mapping with real opencode IDs (ceo qwen3.7-max+kimi-k2.6,
  cto kimi-k2.7-code, ciso glm-5.2; developer=claude, reviewer=codex, qa/doc=gemini).
- **Governance**: the role prompts read Noxys's `CLAUDE.md`, `AGENTS.md`,
  `AGENT-GOVERNANCE.md`, `.cursorrules`, `.windsurfrules`, `GEMINI.md`, and the vault
  `AGENT-DETECTION-FABRIC.md` — all present.

> ⚠️ Sovereignty note: Noxys's `AGENTS.md` mandates *European-sovereign-first*, but
> the configured models (qwen/kimi/glm) are non-EU. This was an explicit choice
> (perf over sovereignty for now). To switch later, add `role_overrides` /
> `allowed_runners` for `noxys` in `policies.yaml` — no code change.

## Prerequisites for a real run

The agent CLIs must be authenticated for their models:
- **opencode** authenticated for `opencode-go/*` (qwen/kimi/glm),
- **claude**, **codex**, **cursor-agent**, **gemini** logged in.

Check: `hivepilot doctor` (lists the runner CLIs found on PATH).

## Driving Noxys

```bash
# 1. Validate the full wiring — no agent calls, no approval needed:
hivepilot run-pipeline noxys company --simulate

# 2. Real run — queues for approval (allow_auto_git lets the dev stage open a PR):
hivepilot run-pipeline noxys company --auto-git
hivepilot approvals                 # review the pending run
# approve (CLI or Telegram /approve <id>) -> the company executes:
#   CEO debate -> CoS plan -> CTO -> Developer (commits + pushes branch) ->
#   Reviewer (codex reviews, then opens the PR) -> CISO -> QA -> Documentation
#   -> Report -> CEO approval
# You merge the PR.

# 3. A standalone CEO decision:
hivepilot debate noxys "Adopt event-sourcing for the audit log?"
```

From Telegram (after enabling — see [USAGE.md](USAGE.md)): `/runpipeline noxys company`,
`/interactions`, `/steps <run_id>`, `/approve <id>`.

## Relaxing / tightening later

Edit `policies.yaml > projects.noxys`:
- drop `require_approval` to `false` once you trust the runs,
- set `allowed_runners` / `role_overrides` to enforce EU models,
- keep `allow_auto_git` on only if you want agents pushing branches.
