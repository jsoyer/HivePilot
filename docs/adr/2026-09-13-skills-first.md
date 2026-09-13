---
title: Skills-first doctrine — capability vs runtime
type: adr
status: proposed
created: 2026-09-13
agent: hivepilot
language: en
linear: HP-103
amends: HP-94
---

# Skills-first doctrine — capability vs runtime

## Status:

proposed

Depends on [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors)
(accepted). Vault ADR: `Jsoyer/Projects/HivePilot/Decisions/2026-09-13-coworker-openspace-patterns.md`.
Plan SSOT: `Jsoyer/Projects/HivePilot/Docs/2026-09-13-plan-coworker-openspace.md`.

This file records the skills-first split. It does **not** accept HP-94 in
place; it amends it with one ownership rule. Flip `status` to `accepted`
when Jerome signs off (same path as HP-94).

## Context:

HivePilot already ships two skill sources (`docs/SKILLS.md`): plugin
`SkillSpec` and directory `skills/<name>/SKILL.md`. Stages and steps opt in
by name (`PipelineStage.skills` / `TaskStep.skills`;
`hivepilot stage attach-skill`). HP-98 scans those sources read-only into a
revision DAG. HP-79 workshop writes directory skills only after HITL.

Coworker’s AGENTS.md / repo-contracts (patterns only; no TS/Electron
vendoring) treat **marketplace / project / user skills as the
authoritative capability content**. The unified runtime is a separate
executable layer: confinement, approvals, and audit. It must never be
registered as a plugin or skill discovery root. Domain procedures that
live in the harness as prompt constants rot into a second, unversioned
skill catalog.

HivePilot’s orchestrator and services still embed agent procedures as
Python strings (review challenge, debate judge, lessons distill,
concierge classifier, rebuttal protocol). Those are capabilities. The
runtime already owns the parts that should stay: worktrees / bwrap,
HP-61 / HP-97 PASS inbox, HP-40 `change_log`, HP-95 tool catalog, HP-99
evidence, secrets masking.

HP-108 (skill→tools gate for concierge/chat) depends on this doctrine
and is implemented as a chat-only capability map. Pipeline stage-attach
stays the attach surface.

## Options:

- Keep writing agent procedures as Python string constants in
  `orchestrator.py` / services. Fast today; a second unpublished skill
  catalog tomorrow.
- Steal Coworker’s split only: **capability = `SKILL.md` + scripts;
  runtime = confinement / approvals / audit.** Rewrite as HivePilot
  docs. Do not vendor Coworker.
- Change `PipelineStage.skills` to implicit / keyword force-load
  (HP-108 shape). Rejected here — attach stays explicit YAML.
- Fold this into HP-94 in the vault only. Rejected — in-repo ADRs live
  under `docs/adr/` per `docs/adr/README.md`.

## Decision:

**Capability lives in `SKILL.md` plus optional scripts and references.
The HivePilot runtime is confinement, approvals, and audit.**

| Layer | Owns | Does not own |
| --- | --- | --- |
| **Capability** | How-to: procedures, output contracts, exploration perimeters, editorial rules. Shipped as directory `SKILL.md` (and `scripts/` / `references/`) or plugin `SkillSpec.files` + `system_prompt`. | Gates, isolation, durable log, catalog policy |
| **Runtime** | Worktree / bwrap confinement; HP-61 cards + HP-97 PASS (`kind` ∈ {partition, tool, memory, skill_evolution}); HP-40 / HP-99 audit; HP-95 risk×policy; HP-98 read-only scan; secrets masking; fail-closed parse / clamp | Domain playbooks encoded as prompt constants |

Rules:

1. **Skills are the capability SSOT.** A new agent procedure is a skill
   (or an update to one), not a new `_PROMPT_TEMPLATE` in the engine.
   `improve` / `shadcn` bundled plugins already follow this shape.
2. **Runtime is not a skill root.** Engine trees (`hivepilot/`, bundled
   Python) stay off the `skills/` scan roots. Scan roots remain
   working-directory, config-repo, and `$XDG_DATA_HOME/hivepilot/skills`
   (`docs/SKILLS.md`).
3. **Attach stays explicit.** `PipelineStage.skills` / `TaskStep.skills`
   and `hivepilot stage attach-skill` / `detach-skill` are unchanged.
   No keyword force-load, no concierge auto-attach (HP-108).
4. **Patterns only.** Coworker `SKILL.md` + scripts ownership and
   “runtime ≠ discovery root” are rewritten here. No vendored TS,
   Electron, or pickle (HP-94 licences).
5. **Follow-ups stay tickets.** HP-108 maps skill → tool tokens for
   concierge/chat (`hivepilot/skill_capabilities.py`). HP-105 trust,
   HP-104 skill-cycle events, HP-100 idempotency, HP-101 memory HITL,
   HP-102 presenter parity are cited only.

Read-only evidence of today’s debt:
[runtime-to-skill audit](../runtime-to-skill-audit.md) (top 5). That
note implements nothing.

## Consequences:

Positive:

- One sentence operators and later tickets can point at: capability vs
  runtime.
- Stage-attach and the four Telegram doors stay where HP-94 left them.
- HP-108 gates concierge/chat tool tokens against this doctrine
  (skill off → tools absent on that surface only).

Negative / follow-up:

- The five audit items stay in Python until later extract tickets.
  This ADR does not move them.
- Role `prompt_file` markdown (`prompts/agents/*.md`) is adjacent
  capability, already on disk, not attached via `skills:`. Not in the
  top 5; do not silently rewire roles here.
- Status stays `proposed` until Jerome accepts.

### Follow-up ticket titles (do not open here)

- Extract adversarial-review procedure from `orchestrator.py` into
  `skills/adversarial-review/` (audit #1)
- Extract distill-lessons prompt from `lessons_service.py` (audit #2)
- Extract debate-judge / challenge-arbiter prompts (audit #3)
- Extract challenge-rebuttal protocol (audit #4)
- Extract concierge-classifier instructions (audit #5); HP-108 owns
  the skill→tools gate (classifier stays no-tools)

## Security Impact:

- No runtime behavior change. No new tool surface. No WhatsApp. No
  HP-67 desktop-per-agent sandbox.
- Moving a procedure *later* must leave parse / clamp / redaction /
  PASS / destructive-op fail-closed in the engine. A skill must not
  silently widen `allowed-tools`, hooks, or permissions (HP-110).
- Four Telegram doors and HP-94 inbox `kind` set are untouched.

## Review Date:

2026-10-13
