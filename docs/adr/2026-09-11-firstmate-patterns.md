---
title: Firstmate patterns only — no product integration
type: adr
status: accepted
created: 2026-09-11
agent: hivepilot
language: en
linear: HP-84
---

# Firstmate patterns only — no product integration

## Status:

accepted

## Context:

[HP-84](https://linear.app/js-workspace/issue/HP-84/patterns-firstmate-a-piquer-pas-dintegration-produit) records the 2026-09-10 comparison of [kunchenguid/firstmate](https://github.com/kunchenguid/firstmate) with HivePilot.

Firstmate is a **terminal agent distro** (AGENTS.md + scripts + git worktrees) aimed at a solo captain. HivePilot is already the **command center**: YAML-driven orchestrator, runners, missions, Pollen, Hindsight, MCP, approval gates, and the HP-40 event bus.

This is the same product stance as [HP-71](https://linear.app/js-workspace/issue/HP-71/integrer-hermes4-comme-providermodele-nous-portal-openrouter) on the Hermes Agent framework: **do not embed a second control plane**. HP-71 took Hermes-4 as an OpenAI-compat *model* behind `model_profiles.yaml` / the `openrouter` runner and left the Hermes Agent framework out. Firstmate has no equivalent "just a model" slice — only overlapping orchestration patterns.

This ADR is **not** a code port of Firstmate. It records the no-integration decision, ranks five candidate patterns, and names the next spikes. Using Firstmate *beside* HivePilot to ship this repo personally remains allowed and is out of product scope.

Existing HivePilot pieces the ranking must respect:

- **HP-40 event bus** (`hivepilot/services/events.py`) — durable `change_log` plus Postgres `LISTEN/NOTIFY` wakeup; `emit()` is fail-safe; SSE is HP-41.
- **HP-50 nudge** (`hivepilot/services/nudge_engine.py`) — CI / review / conflict → structured verdict + space post; **must never start an LLM reply loop** or change a gate.
- **HP-61 approvals** (`approval_rules_service.py`, inline chat cards) — per-action `approve` / `deny` rules; no match → human; destructive ops already fail-closed.
- **Worktrees** — `git_service.isolated_worktree` (throwaway `<repo>/.hivepilot-wt/<uuid>`, default on via `HIVEPILOT_WORKTREE_ISOLATION`); HP-69 `pipeline` strategy already says parallel worktrees; mid-task approval + worktree isolation is an explicit incompatibility.
- **Runners + `model_profiles.yaml`** — dispatch precedence `policy > stage > role > runner-default`; effort enum; HP-70 per-mission `roles_config` fallback; HP-71 Hermes-4 as the OSS column.

## Options:

Product stance (pick one):

- Embed Firstmate as a runtime (`kind: firstmate`, clone, or subprocess harness).
- Port Firstmate surfaces into HivePilot (tmux / Herdr captain loop, Secondmates, Relay X-Discord).
- Steal patterns only; no product integration; no Firstmate dependency (same stance as HP-71 vs the Hermes Agent framework).
- Ignore Firstmate entirely.

Candidate patterns to steal (rank; do not implement in this ADR):

1. **Zero-token event-driven watcher** — a sleeper that wakes the captain only on actionable events (no LLM poll). Firstmate's bash watcher is the reference shape. Cross with HP-40 (wakeup + durable log) and HP-50 (observe without starting a model).
2. **Worktree isolation for parallel coding missions** — one worktree per ship mission so concurrent runners do not collide on one checkout (Firstmate / [treehouse](https://github.com/kunchenguid/treehouse) style). Related to P7 sandbox / computers only if we push further.
3. **Ship vs scout contracts** — ship = authorized change + a delivery path; scout = standalone report, no implicit merge. Clarifies investigation vs shipping missions.
4. **Explicit escalation** — auto-fix mechanical work; ask a human on product forks / destructive / security. Aligns HP-61 cards and policies.
5. **Dispatch harness / model / effort by task class** — Firstmate-style `crew-dispatch.json` profiles. Crosses existing runners, `model_profiles.yaml`, and HP-70 right-sizing.

Out of scope (do not ticket as product work):

- Runner `kind: firstmate` or any runtime dependency on a Firstmate clone.
- Rewriting Pollen around tmux / Herdr.
- Secondmates / Relay X-Discord from Firstmate.

## Decision:

**Steal patterns only. Do not integrate Firstmate as a product.** No `kind: firstmate`, no vendored Firstmate tree, no second orchestrator. Personal use of Firstmate next to this repo is fine and stays off the product roadmap.

### Ranking

| Rank | Pattern | Why this rank |
| --- | --- | --- |
| 1 | Zero-token event-driven watcher | Highest leverage gap. HP-40 already is a wakeup + durable log; HP-50 already forbids an LLM reply loop. What is missing is an *idle* consumer that classifies bus kinds as actionable vs noise **without tokens** and only then wakes orchestrator / nudge / a human. Today's nudges fire in-process from git hooks; the scheduler and autopilot still tick on a clock. A Firstmate-shaped sleeper on `subscribe()` is the natural next consumer of the bus. |
| 2 | Explicit escalation (mechanical auto-fix / ask on product forks) | HP-61 matches `project` / `task` / `action` strings and otherwise waits for a human. Destructive ops are already gated. The missing piece is a **change class**: mechanical (lint, lockfile, changelog typo) may auto-fix; product-fork / security / destructive must ask. A watcher that wakes on events needs this split or it will either auto-run too much or page too often. Composes with existing approval cards — no new control plane. |
| 3 | Ship vs scout contracts | HivePilot already has dry-run, debate/PR gates, "partitions never auto-merge", and autopilot that never chooses *what* matters. There is still no first-class mission contract that says scout cannot merge and ship must name a delivery path. Valuable, but it is mostly a YAML / policy convention once #1 and #2 exist. Do it next, not as the first spike. |
| 4 | Dispatch harness / model / effort by task class | Role-level dispatch already exists (`model_profiles.yaml`, `role_profiles`, effort enum, policy overrides, HP-70 `roles_config`). Task-*class* dispatch (scout vs ship vs review vs mechanical-fix) is an incremental map onto that machinery. Wait until ship/scout classes (#3) are named so we do not invent a parallel profile file. |
| 5 | Worktree isolation for parallel coding missions | **Already shipped** as throwaway per-run worktrees (`isolated_worktree`, default on) and called out on the HP-69 `pipeline` strategy. HP-84's "probably spike worktrees" hunch assumed a green field. The remaining work is verification (do parallel partition / mission dispatches still share a checkout when isolation falls back in-place?) plus the known mid-task approval vs worktree incompatibility — not a Firstmate port. |

HP-84 suggested watcher + worktree as the likely pair. We keep the watcher and **swap worktrees for escalation**: worktrees are in-engine; escalation is the policy gap that makes a zero-token wake *safe*.

### Spikes (top 1–2 only)

1. **Zero-token actionable-event consumer on the HP-40 bus** — subscribe / tail `change_log`; allowlist kinds (e.g. `nudge.posted`, approval requested, run failed); sleep otherwise; never call a model to decide whether to wake.
2. **Mechanical vs product-fork classes on HP-61 rules** — a small taxonomy bound to existing `approve` / `deny` / pending cards; unknown class fail-closed to "ask".

Do not implement either spike in the same change as this ADR.

## Consequences:

Positive:

- One recorded "no second control plane" rule for both Hermes Agent (HP-71) and Firstmate (HP-84).
- Next engineering work is two thin spikes on pieces we already own (bus, nudge, approval rules), not a framework import.
- Worktree effort stays a verify/harden ticket instead of a rebuild.

Negative / follow-up:

- Operators who want Firstmate's captain-in-tmux UX will keep using Firstmate beside HivePilot; Pollen will not grow that loop.
- Ranking #5 will surprise anyone who only read the HP-84 "probably worktrees" note — point them here.
- Ship/scout and task-class dispatch stay undocumented in YAML until their later tickets.

### Follow-up ticket titles (create later)

Do not open these as part of landing this ADR. Titles only:

- Spike: zero-token actionable-event consumer on the HP-40 bus (no LLM poll)
- Spike: bind mechanical auto-fix vs product-fork classes to HP-61 approval rules
- Spec: ship vs scout mission contracts (no implicit merge on scout)
- Task-class dispatch: map ship / scout / review / mechanical-fix to runner + model_profile + effort
- Verify: parallel coding missions never share a checkout under partition dispatch
- Verify: nudge/orchestrator can wake from bus kinds (`nudge.posted`, approval requested, run failed) without in-process-only hooks

## Security Impact:

- **Not** embedding Firstmate avoids a second agent runtime, a second secrets/MCP surface, and a second place that can mutate git. Same supply-chain posture as leaving the Hermes Agent framework out (HP-71).
- Spike 1 must stay fail-safe like `events.emit` and HP-50: a broken or noisy consumer must not change a gate, start a model, or drop durable `change_log` facts. Wake allowlists fail closed (unknown kind = sleep).
- Spike 2 must fail closed: unknown or contested class → human (HP-61 pending), never auto-approve. Mechanical auto-fix must not be inferred from "the watcher woke". Destructive / outward / `merge_pr` invariants in `INVARIANTS.md` stay untouched.
- This ADR changes no runtime behavior.

## Review Date:

2026-10-11
