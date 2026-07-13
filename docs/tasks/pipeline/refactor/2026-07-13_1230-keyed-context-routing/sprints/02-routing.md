# Sprint 2: Routing + context_routing_mode opt-in

> Self-contained. Load ONLY this file. Part of PRD A2 (keyed context routing).
> Repo: `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot` (Python, Pydantic v2, pytest, ruff+mypy py3.12).

## Objective

Add `context_routing_mode` (default `full`). In `keyed` mode, a stage whose role declares `inputs`
gets a prior context assembled from ONLY those keys (from Sprint 1's `outputs_by_key` store), with a
conservative fallback. `full` mode is unchanged and byte-identical to today.

## Effort: M · Dependencies: Sprint 1 · Model: sonnet

## Code anchors (verified)

- Prior context computed `orchestrator.py:1128-1132` via `build_prior_context(prior_chunks, mode=settings.prior_context_mode, max_chars=settings.max_prior_context_chars)` → passed as `prior_context=` into the stage task.
- `build_prior_context` (`:223-256`) — reuse its `max_chars` cap for the keyed slice too.
- Injection hook: `claude_runner._build_prompt:200-232` reads `payload.metadata.get("prior_context")`. We only change WHAT string is passed as `prior_context`, not the runner.
- Settings live in `hivepilot/config.py` (env-overridable; `prior_context_mode` default `cap` at `:123` is the pattern to follow).
- Sprint 1 added the run-scoped `outputs_by_key: dict[str,str]` store and `_parse_output_sections`.

## ⚠️ Backward-compat trap (critical)

`roles.yaml` ALREADY declares `inputs` on every role (cosmetically). Routing MUST be gated by the
explicit `context_routing_mode` flag, NEVER by input-presence — else every existing pipeline silently
regresses to a keyed subset. Default `full` = today's behaviour for ALL roles regardless of inputs.

## File Boundaries

files_to_create:
- (none)

files_to_modify:
- `hivepilot/config.py`
- `hivepilot/orchestrator.py`
- `tests/test_pipeline_execution.py`
- `tests/test_config.py`

### Read-Only & Shared Contracts
- read-only: hivepilot/runners/claude_runner.py
- shared_contracts: context_routing_mode values (full|keyed, default full); conservative fallback rules

## Tasks

- [x] Add `Settings.context_routing_mode: Literal["full", "keyed"] = "full"` in `config.py` (env-overridable, mypy/pydantic-clean).
- [x] At `orchestrator.py:1128`, branch the prior_context computation: if `settings.context_routing_mode == "keyed"` AND the consuming stage's role has non-empty `inputs` → assemble prior_context from `{k: outputs_by_key[k] for k in role.inputs if k in outputs_by_key}` (join with `## <KEY>` headers for readability; apply the same `max_chars` cap). Otherwise → today's `build_prior_context(prior_chunks, ...)` unchanged.
- [x] Conservative runtime fallback: in keyed mode, if NONE of the role's input keys are present in the store (would yield empty context), fall back to full `build_prior_context(prior_chunks, ...)` and log a warning naming the missing keys. (Decide: partial-present = use what's present; all-missing = full fallback. Document.)
- [x] Tests: (a) `full` mode → prior_context byte-identical to pre-change for a multi-stage pipeline (regression); (b) keyed mode → a stage with `inputs:[design_spec, technical_spec]` receives ONLY those, and its assembled context is strictly smaller than full prior_chunks; (c) missing-key → full fallback + log; (d) role with empty inputs in keyed mode → full context.

## Acceptance Criteria

- [x] `context_routing_mode` defaults `full`; full mode unchanged for all roles (regardless of declared inputs).
- [x] Keyed mode routes only declared inputs; conservative fallback never yields empty context.

## Verification

- [x] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py tests/test_config.py`
- [x] `python -c "from hivepilot.config import Settings; assert Settings().context_routing_mode=='full'"`
- [x] Full suite green; ruff + mypy clean on changed files.

## Agent Notes

**Decisions made:**
- `_route_prior_context()` (new module-level helper in `orchestrator.py`, placed
  directly after `build_prior_context`) centralizes the routing decision so the
  call site in `run_pipeline` stays a single expression. Gate is
  `routing_mode == "keyed" and role is not None and role.inputs` — matches the
  spec's explicit instruction that gating must be on the flag only, never on
  input-presence alone (the flag check is evaluated first / short-circuits, so
  in `full` mode `role.inputs` is never even inspected).
- Conservative fallback rule implemented exactly as specified: partial-present
  (>=1 of the declared keys found) → use only what's present, no fallback.
  All-missing → fall back to `build_prior_context(prior_chunks, ...)` and
  `logger.warning("pipeline.keyed_context_fallback", stage=..., missing_keys=...)`.
  Role with empty `inputs` list also falls through to full context (same
  code path as `full` mode, since `role.inputs` is falsy).
- Keyed slice formatting: `"## <KEY_UPPER>\n<content>"` blocks joined with
  `"\n\n"`, same tail-truncation cap (`…[earlier context truncated]…` prefix)
  as `build_prior_context`'s `"cap"` mode, reusing `settings.max_prior_context_chars`.
- Consuming role lookup added right before the `run_task` call, reusing the
  exact `self.tasks.tasks.get(stage.task).role -> ROLES.get(role)` pattern
  Sprint 1 used for the *producing* role after the call — same `stage.task`
  value, just resolved earlier in the loop for the "about to run" stage.
  Local `from hivepilot.roles import ROLES` import kept (not hoisted to module
  level) to match Sprint 1's existing style and stay patchable the same way
  in tests (`patch("hivepilot.roles.ROLES", {...})`).
- Updated the Sprint-1 comment above the `outputs_by_key` population block —
  it previously said "isn't consumed yet" / "byte-identical to pre-sprint",
  which is now stale since Sprint 2 consumes it in keyed mode. Reworded to
  clarify the store is consumed only when `context_routing_mode="keyed"`.

**Assumptions:**
- 🟢 HIGH — `settings` is a single module-level singleton (`hivepilot.config.settings`)
  imported by reference into `orchestrator.py`; tests monkeypatch attributes on
  it directly (`monkeypatch.setattr(orchestrator_settings, "context_routing_mode", "keyed")`)
  for auto-revert, following no prior in-file precedent but standard pytest
  practice.
- 🟢 HIGH — `structlog`'s bound logger object (`hivepilot.orchestrator.logger`)
  supports `patch.object(logger, "warning")` for assertion in tests; verified
  by running the fallback test, which passed.
- 🟡 MEDIUM — Chose to key-slice-format headers as `## <KEY_UPPER>` (e.g.
  `## DESIGN_SPEC`) to mirror the existing `_parse_output_sections` header
  convention, though the spec only said "join with `## <KEY>` headers for
  readability" without specifying case. Not consumer-visible in a
  machine-parseable way (it's prose fed to an LLM), so low risk either way.

**Issues found:** none outside sprint scope. No files needed changes outside
the declared boundaries (`hivepilot/config.py`, `hivepilot/orchestrator.py`,
`tests/test_pipeline_execution.py`, `tests/test_config.py`).

**Anti-Goodhart check:** Tests assert on the actual *content* of the routed
string (substring checks for `"design body"` present, `"trailing"`/`"intro
prose"`/`"unrelated section"` absent, length comparisons), not just that a
call happened or that some non-None value was returned. The full-mode
regression test runs the real pipeline twice with differing `role.inputs`
and asserts byte-equality — this is the strongest test against the
backward-compat trap because it can't be gamed by hardcoding an expected
string; it fails if routing ever keys off `role.inputs` presence instead of
the mode flag.
