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
> **`default`** (planning → plan checkpoint → dev → … → Jules approves the PR)
> is documented in [USAGE.md](USAGE.md).

## The chain (pipeline `company`)

| # | Stage | Task | Role | Runner | Model | git |
|---|---|---|---|---|---|---|
| 1 | CEO Intake | acme-ceo-intake | ceo | opencode | qwen3.7-max **+** kimi-k2.6 → **debate→ADR** | — |
| 2 | Chief of Staff Plan | acme-cos-plan | chief_of_staff | cursor | (default) | — |
| 3 | CTO Review | acme-cto-review | cto | opencode | kimi-k2.7-code | — |
| 4 | Implementation | acme-developer | developer | claude | (default) | commit + push **branch** |
| 5 | Review | acme-reviewer | reviewer | **codex** | (default) | **review → open PR** |
| 6 | Security | acme-ciso | ciso | opencode | glm-5.2 | — |
| 7 | QA | acme-qa | qa | **cursor** | (default) | — |
| 8 | Documentation | acme-documentation | documentation | **gemini** | (default) | commit |
| 9 | Report | acme-cos-report | chief_of_staff | cursor | (default) | — |
| 10 | Approval | acme-ceo-approval | ceo | opencode | qwen3.7-max + kimi-k2.6 → **debate→ADR** | — |

So: **codex** does code review, **gemini** does documentation, **claude**
implements, **opencode** drives the strategy/security roles (qwen/kimi/glm), and
**cursor** drives QA + coordination (a dedicated QA runner, separate from docs).

## Code review & pull requests

- **Developer (claude)** implements, commits, and pushes the branch
  `hivepilot/<project>` — **no PR yet**.
- **Reviewer (codex)** reviews the pushed branch, then **opens the PR**
  (`git_service.create_pr` via `gh`) — so review happens **before** the PR exists.
  Set `git.draft: true` on the reviewer's task to open it as a **draft PR**
  instead of a regular one (draft-then-promote flow, next section).
- A later stage may **merge** the PR autonomously via `git.merge_pr` (Jules'
  final approval — GitHub forbids approving your own PR, so merge is the
  actionable step in a solo workflow) — or a **human merges** it manually.
- This only happens when the run is invoked with `--auto-git` **and** the
  project policy has `allow_auto_git: true`. With `require_approval: true`
  (e.g. acme) the run still waits for a human `/approve` before anything runs.

## Draft PR, then promote-to-ready at the release gate

A task's `git` block can open its PR as a **draft** (`create_pr: true, draft:
true`) — visible on GitHub but excluded from review queues/CI-required-checks
until someone (or some stage) marks it ready. A later **release-gate** stage
(any `can_block` role — CTO, Reviewer, CISO, QA) then **promotes** it via
`git.promote_pr: true` (`gh pr ready <branch>`), typically alongside
`merge_pr: true` to also merge once ready.

**Promotion/merge run unless the gate stage reports an explicit blocking
status.** Every role's prompt (`prompts/agents/*.md`) requires a free-text
`status:` verdict, and `hivepilot.services.agent_report.parse_agent_report`
already extracts that `status` field — but historically nothing consumed it:
the pipeline's `stage_failed` check (which drives fail-fast) only looks at
whether the *runner* raised, and the same `parse_agent_report(stage_output)`
call in the stage loop is used solely to detect `.challenge` for the
agent-to-agent challenge feature. So a gate stage that reported `BLOCKED` in
its own text would previously still have its PR promoted or merged.
`perform_git_actions` now takes the stage's own `task_result` and skips
`promote_pr` (and, defensively, `merge_pr`) whenever the parsed verdict is an
explicit blocking verdict — `BLOCK | BLOCKED | REJECT | REJECTED |
REQUEST_CHANGES | CHANGES_REQUESTED | NEEDS_HUMAN | FAIL | FAILED | DENY |
DENIED` (logged as `git.promote_skipped_blocked` /
`git.merge_skipped_blocked`). The approval vocabulary is heterogeneous — the
release gate approves with `status: APPROVE`, code roles with `PASS`, security
with `CLEARED`, advisory roles with `ADVISORY` — so a blocking-verdict
**blacklist** (not a PASS-only whitelist) is used: everything that isn't an
explicit block, including all those "proceed" synonyms and any output with no
parseable `status:` (e.g. a plain shell step), promotes. `NEEDS_HUMAN` counts
as blocking — it defers to a human, so the PR stays a draft. This is additive
and does not change behaviour for tasks that aren't `can_block` roles.

Example — CISO as the release gate for a draft PR the Reviewer opened:

```yaml
acme-reviewer:
  role: reviewer
  git: { create_pr: true, draft: true, branch_prefix: hivepilot }

acme-ciso:
  role: ciso
  git: { promote_pr: true, merge_pr: true, merge_method: squash, branch_prefix: hivepilot }
```

If Hugo (CISO) reports `status: BLOCKED`, the PR stays a draft and is not
merged. If he reports `status: PASS`, it is promoted to ready and merged.

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
