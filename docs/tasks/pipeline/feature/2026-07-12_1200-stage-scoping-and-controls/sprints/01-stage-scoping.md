# Sprint 1: Stage scoping + continue_on_failure + skip

> Self-contained. Load ONLY this file. Part of PRD A1 (HivePilot pipeline stage capabilities).
> Repo: `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot` (Python, Pydantic v2, pytest `asyncio_mode="auto"`).

## Objective

Add three optional `PipelineStage` fields + `Group.tags`, implement per-stage skip logic and
`continue_on_failure` activation, with fail-closed tag validation and full tests. Everything is
additive and backward-compatible: a stage with no new fields behaves exactly as today.

## Estimated effort: M · Dependencies: None · Model: sonnet · Parallel with: Sprint 2

## Code anchors (verified)

- `hivepilot/models.py`: `PipelineStage` (~95-99) currently only `name`, `task`, `pause_before: bool=False`, `commits_vault: bool=False`. `Group` (~111-117) only `description`, `hub`, `components: list[str]`. Pydantic v2 `extra="ignore"` — new fields MUST be real fields.
- `hivepilot/orchestrator.py`: stage loop ~927-1005. `selected_components: list[str]` init at :940, narrowed by `_parse_components(...)` at :946-949 — **already in scope** inside `for stage_idx, stage in enumerate(pipeline.stages)`. Fail-fast at :1183-1192: `stage_failed = any(not r.success ...)` then `if stage_failed and not getattr(stage, "continue_on_failure", False): ... break`. The `getattr` is a latent/dead hook (field does not exist yet).
- `hivepilot/services/state_service.py:32-55`: `RunStatus(str,Enum)` is **run-level** — do NOT add a stage skip here.
- The run's `Group` (with `.tags`) is available where `selected_components` is resolved; use it to resolve `only_tags`.

## File Boundaries

files_to_create:
- (none)

files_to_modify:
- `hivepilot/models.py`
- `hivepilot/orchestrator.py`
- `tests/test_pipeline_execution.py`
- `tests/test_models.py`
- `tests/test_component_selection.py`

### Read-Only & Shared Contracts
- read-only: hivepilot/services/state_service.py, hivepilot/config.py, groups.yaml, pipelines.yaml
- shared_contracts: PipelineStage field names + skip semantics (frozen — PRD B depends on them)

## Shared contract (FROZEN — do not rename)

- `PipelineStage.only_components: list[str] | None = None`
- `PipelineStage.only_tags: list[str] | None = None`
- `PipelineStage.continue_on_failure: bool = False`
- `Group.tags: dict[str, list[str]] = {}`  (tag → component names)
- **Skip semantics:** target = `set(only_components or []) ∪ {c for t in (only_tags or []) for c in group.tags[t]}`. Skip iff target is non-empty AND disjoint from `selected_components`. A skipped stage: task NOT invoked, NOT counted as failure, `prior_chunks` untouched, run continues. Stage with neither selector always runs.

## Tasks

- [x] Add the four fields above (`PipelineStage` ×3, `Group` ×1) with the exact names/defaults.
- [x] In the stage loop, before running a stage's task, compute the target component set and skip per the contract. Represent the skip at stage level (a `skipped` flag on the stage result, or a sentinel) — decide the representation but honour the contract (neither success nor failure; `prior_chunks` untouched).
- [x] Fail-closed validation: an `only_tags` value not present in the run's `Group.tags` raises a clear error (ValueError with the offending tag) at the earliest point holding both stages and group tags. Do NOT silently skip on unknown tag.
- [x] Now that the field exists, keep/simplify the `getattr(stage, "continue_on_failure", False)` at :1184 (can become `stage.continue_on_failure`). Confirm fail-fast is preserved when the flag is false/absent.
- [x] Tests in `tests/test_pipeline_execution.py` (and `test_component_selection.py` as fitting): skip-excludes, no-skip-matches, no-selector-always-runs, `only_components` match, `only_tags` match, union-of-both, undefined-tag-raises, `continue_on_failure=true` suppresses break, `continue_on_failure=false/absent` preserves fail-fast, skipped-stage-not-in-prior_chunks.
- [x] Model tests in `tests/test_models.py`: defaults are `None/None/False/{}`.

## Acceptance Criteria

- [x] All PRD §6 criteria except the two `Role.prompt_file` ones (Sprint 2) and the docs one (Sprint 3).
- [x] Backward-compat: existing pipeline tests unchanged and green.

## Verification

- [x] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py tests/test_models.py tests/test_component_selection.py tests/test_company_pipeline.py`
- [x] `python -c "from hivepilot.models import PipelineStage, Group; s=PipelineStage(name='x',task='t'); assert s.only_components is None and s.only_tags is None and s.continue_on_failure is False; g=Group(description='d',hub='h',components=[]); assert g.tags=={}"`
- [x] Full suite green: `python -m pytest -q`

## Orchestrator Integration Note (Batch 1 merge)

- Sprint 1 added a `group_tags` kwarg to `Orchestrator.run_pipeline()` but `cli.py` was outside
  its file boundaries, leaving `only_tags` non-functional end-to-end (a group-mode run would hit
  the fail-closed validator and raise, since `group_tags` defaulted to `{}`).
- **Integration fix applied by the orchestrator (Team Lead):** `hivepilot/cli.py` group-mode
  `run_pipeline(...)` call now passes `group_tags=grp.tags`. The non-group call is unchanged
  (no group → `group_tags` correctly defaults to `{}`). This wires the feature through to the CLI
  so PRD B can actually consume `only_tags`. Verified: full suite 777 passed, 0 new failures,
  ruff + mypy clean on models.py/orchestrator.py/roles.py/cli.py.
