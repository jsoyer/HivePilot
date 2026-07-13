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

- [ ] Add `Settings.context_routing_mode: Literal["full", "keyed"] = "full"` in `config.py` (env-overridable, mypy/pydantic-clean).
- [ ] At `orchestrator.py:1128`, branch the prior_context computation: if `settings.context_routing_mode == "keyed"` AND the consuming stage's role has non-empty `inputs` → assemble prior_context from `{k: outputs_by_key[k] for k in role.inputs if k in outputs_by_key}` (join with `## <KEY>` headers for readability; apply the same `max_chars` cap). Otherwise → today's `build_prior_context(prior_chunks, ...)` unchanged.
- [ ] Conservative runtime fallback: in keyed mode, if NONE of the role's input keys are present in the store (would yield empty context), fall back to full `build_prior_context(prior_chunks, ...)` and log a warning naming the missing keys. (Decide: partial-present = use what's present; all-missing = full fallback. Document.)
- [ ] Tests: (a) `full` mode → prior_context byte-identical to pre-change for a multi-stage pipeline (regression); (b) keyed mode → a stage with `inputs:[design_spec, technical_spec]` receives ONLY those, and its assembled context is strictly smaller than full prior_chunks; (c) missing-key → full fallback + log; (d) role with empty inputs in keyed mode → full context.

## Acceptance Criteria

- [ ] `context_routing_mode` defaults `full`; full mode unchanged for all roles (regardless of declared inputs).
- [ ] Keyed mode routes only declared inputs; conservative fallback never yields empty context.

## Verification

- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py tests/test_config.py`
- [ ] `python -c "from hivepilot.config import Settings; assert Settings().context_routing_mode=='full'"`
- [ ] Full suite green; ruff + mypy clean on changed files.
