---
title: Runtime → SKILL audit (top 5)
type: audit
status: read-only
created: 2026-09-13
linear: HP-103
basis: origin/main@3e62767
---

# Runtime → SKILL audit (top 5)

Read-only snapshot for [HP-103](https://linear.app/js-workspace/issue/HP-103/u-09-doctrine-skills-first-audit-top-5-runtimeskill).
Doctrine: [docs/adr/2026-09-13-skills-first.md](./adr/2026-09-13-skills-first.md)
(capability = `SKILL.md` + scripts; runtime = confinement / approvals / audit).

This note **does not** extract anything, change
`PipelineStage.skills` / `hivepilot stage attach-skill`, or implement
HP-108. Line numbers are from `origin/main` at merge
`3e62767` (HP-99). Re-check before any later extract ticket.

## Method

Inspected orchestrator prompt constants, lessons / concierge services,
bundled skill plugins, `prompts/`, playbooks, autopilot, nudge, and PR
body helpers. Kept in runtime: gates, isolation, audit, catalogs,
secrets, stage-attach, HP-100/101/102/105/108 paths (cite only).

Already-correct pattern (not in the top 5):
`hivepilot/bundled_plugins/improve.py` and `shadcn.py` — plugin
`SkillSpec` with embedded `SKILL.md` + `system_prompt`.

## Top 5 — capability still in the runtime

### 1. Adversarial PR/diff review

| | |
| --- | --- |
| **Where** | `hivepilot/orchestrator.py`: `_REVIEW_CHALLENGE_PROMPT_TEMPLATE` (~1263–1297), perimeter constants (~1337–1377), `_build_review_challenge_prompt()` (~1427–1455), `_run_review()` |
| **Encoded** | “Find every reason to reject,” mandatory `status:` line, and which exploration perimeter the reviewer may use (whole / truncated / omitted generated files). Token-budget commentary lives in the same constants. |
| **Why a skill** | How a reviewer behaves on a diff. Role file `prompts/agents/reviewer.md` is mission/format; the orchestrator re-implements perimeter doctrine via `extra_prompt`. |
| **Later skill** | `adversarial-review` |
| **Runtime keeps** | Reviewer dispatch, `_parse_reviewer_verdict`, fail-closed `_register_verdict`, untrusted-input marking, subject file write/cleanup, `diff_filter` size caps |
| **Confidence** | High |

### 2. Lessons distillation

| | |
| --- | --- |
| **Where** | `hivepilot/services/lessons_service.py`: `_DISTILL_PROMPT_TEMPLATE` (~108–136), `build_distill_prompt()` (~272–317), `_format_verdicts` / `_format_interactions` / `_format_outcomes` / `_format_failures`, `distill_lessons()` |
| **Encoded** | Which signals matter (failures strongest), JSON lesson shape, category tags, “prefer fewer, high-quality lessons.” |
| **Why a skill** | Analytical procedure. Redaction around the LLM call is egress control (runtime); the editorial rules are capability. |
| **Later skill** | `distill-lessons` |
| **Runtime keeps** | `has_distillable_signal()`, `redact_text()`, `parse_distilled_lessons()`, `validate_lesson()` / `OutcomeSignal` (anti-poisoning), `record_lesson` |
| **Confidence** | High |

### 3. Debate judge and challenge arbiter

| | |
| --- | --- |
| **Where** | `hivepilot/orchestrator.py`: `_JUDGE_PROMPT_TEMPLATE` + `_build_judge_prompt()` (~1075–1095), `_CHALLENGE_ARBITER_PROMPT_TEMPLATE` + `_build_challenge_arbiter_prompt()` (~1206–1239), `_adjudicate()` / `_adjudicate_challenge()` |
| **Encoded** | Two playbooks: synthesize positions into `{decision, confidence, per_role_stance}`; adjudicate challenge/rebuttal as `ACCEPT`/`DEFEND`. “Never fabricate.” |
| **Why a skill** | Arbitration how-to. Runtime should invoke a runner, parse JSON, apply confidence thresholds, persist verdicts. |
| **Later skill** | `debate-judge` (optional sibling `challenge-arbiter`) |
| **Runtime keeps** | `_parse_verdict`, `resolve_debate_config`, confidence gating, `record_verdict`, simulate short-circuit, `DebateService` majority fallback |
| **Confidence** | High |

### 4. Challenge / rebuttal / cross-agent request protocol

| | |
| --- | --- |
| **Where** | `hivepilot/orchestrator.py`: `_run_rebuttal_round()` (~3701+), inline `rebuttal_prompt` (~3786–3795), `resolution_prompt` (~3889–3897), `request_prompt` (~3646–3651) |
| **Encoded** | Social protocol: target replies `ACCEPT`/`DEFEND`/`ESCALATE`; challenger resolves `ACCEPT`/`MAINTAIN`; separate concise-answer template for agent-to-agent Q&A. Word limits hardcoded. |
| **Why a skill** | Workflow instructions for disagreement. Escalation when `MAINTAIN` hits a human is a gate (runtime). |
| **Later skill** | `challenge-rebuttal` |
| **Runtime keeps** | Upstream target checks, bounded rounds, `_resolve_challenge_via_arbiter`, interaction logging, notification streaming, `max_agent_requests` |
| **Confidence** | Medium–high (smaller than #1/#3; distinct from arbiter playbook) |

### 5. ChatOps concierge classifier

| | |
| --- | --- |
| **Where** | `hivepilot/prompts/concierge.md` (full contract), `hivepilot/services/concierge_service.py`: `_HARDCODED_FALLBACK_PROMPT_TEXT`, `_build_classifier_prompt()`, `route()`, `_clamp()` / destructive-action table |
| **Encoded** | ANSWER vs ROUTE vs ACTION vs MULTI_ROUTE, JSON schema, `follow_up` rules, grounding against roster/history. Packaged markdown is already a skill-shaped file sitting outside `skills/`. |
| **Why a skill** | Operational know-how for “what happens next.” Fail-closed parse, no-tools invariant, and confirmation gating stay runtime. |
| **Later skill** | `concierge-classifier` |
| **Runtime keeps** | `chatops_concierge_enabled`, classifier no-tools / timeout, `_clamp()` against known roles/projects, never trust model `destructive`, pending-offer resolution. **HP-108** later maps skill → tool tokens for concierge/chat only. |
| **Confidence** | High |

## Rejected (look movable, stay runtime)

| Candidate | Why it stays |
| --- | --- |
| `hivepilot/services/pr_body.py` | Forge choke point: resolve `pr_body_file`, redact, size-cap, temp files. Not “how to write a PR.” |
| `nudge_engine.py` / `structured_verdict.py` | Observer on CI/review/conflict. Must never change a gate (HP-50). Audit surface. |
| `lessons_service.validate_lesson()` / `OutcomeSignal` | Fail-closed anti-poisoning from post-hoc signals. Policy, not distill how-to (#2). |
| `autopilot_policy.py` | Budget / allowlist / `require_approval` — confinement (`docs/AUTOPILOT.md`). |
| HP-100 idempotency / pending_tool | Cite only; parallel PR. Runtime resume contract, not a skill. |
| `DebateService` majority fallback | Deterministic tally when no judge decision — algorithm, not playbook. |

## Out of scope (do not treat as extract work here)

- HP-108 skill→tools gate (blocked on this doctrine; pipelines stage-attach still unchanged there too)
- HP-100 / HP-101 / HP-102 / HP-105 code paths
- WhatsApp; HP-67 desktop-per-agent sandbox
- Role `prompt_file` roster (`prompts/agents/*.md`) — already markdown, not Python; attach-via-skills is a later choice, not this audit’s top 5
