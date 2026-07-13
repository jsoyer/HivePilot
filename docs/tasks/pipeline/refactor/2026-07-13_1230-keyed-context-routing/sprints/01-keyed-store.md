# Sprint 1: Keyed store + section extraction

> Self-contained. Load ONLY this file. Part of PRD A2 (keyed context routing).
> Repo: `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot` (Python, Pydantic v2, pytest `asyncio_mode="auto"`, ruff+mypy CI py3.12).

## Objective

Accumulate a run-scoped `dict[str, str]` (output-key → content) as stages run, populated via
`## <KEY>` section extraction with whole-blob coarse fallback. The store is BUILT but NOT consumed
yet — zero behaviour change this sprint.

## Effort: M · Dependencies: None · Model: sonnet

## Code anchors (verified)

- `orchestrator.py:988` `prior_chunks: list[str] = []`; appended `:1145` `prior_chunks.append(f"## {agent} ({stage.name})\n{stage_output}")`; `stage_output` aggregated `:1142-1145` from `RunResult.detail` (flat str; dataclass `:261-269`).
- `_parse_components(text, valid)` (`:157-173`) — precedent: parses a `COMPONENTS:` line, case-insensitive, returns `[]` when none. Mirror its style.
- `Role.outputs` (`roles.py:43-44`, `list[str]`) — the keys to store under. Read-only here.
- Designer prompts emit `## DESIGN_SPEC` / `## UI_REVIEW`; general stages do not → coarse fallback must cover them.

## File Boundaries

files_to_create:
- (none)

files_to_modify:
- `hivepilot/orchestrator.py`
- `tests/test_pipeline_execution.py`

### Read-Only & Shared Contracts
- read-only: hivepilot/roles.py, hivepilot/config.py
- shared_contracts: store keys = producing role.outputs names; `## <KEY>` header convention (case-insensitive); key↔header mapping must handle design_spec ↔ ## DESIGN_SPEC

## Tasks

- [x] Add `_parse_output_sections(text: str, keys: list[str]) -> dict[str, str]` mirroring `_parse_components`: for each key, find a `## <HEADER>` section matching the key (decide+document the mapping — e.g. case-insensitive match of the key with `_`/space/`-` normalization, so `design_spec` matches `## DESIGN_SPEC`), return `{key: section_body}` for those present.
- [x] In the stage loop, after `stage_output` is computed, populate a run-scoped `outputs_by_key: dict[str, str]`: for the producing stage's role, if `_parse_output_sections` finds sections, store those; for any declared output key without a section, store the whole `stage_output` blob (coarse fallback). Later stages overwrite same-key entries (document this).
- [x] Do NOT consume `outputs_by_key` anywhere yet; the `prior_chunks`/`build_prior_context` path is unchanged → behaviour identical.
- [x] Tests in `tests/test_pipeline_execution.py`: `## KEY` section extracted to the right key; coarse whole-blob fallback when no section; a role with multiple outputs and no sections maps the blob to each key; case-insensitive/normalized header match (`design_spec` ↔ `## DESIGN_SPEC`).

## Acceptance Criteria

- [x] Store populated per the section/coarse rules; no consumption yet.
- [x] Existing behaviour byte-identical (full suite green — the store is inert).

## Verification

- [x] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py`
- [x] Full suite: `python -m pytest -q` ; `ruff check hivepilot/orchestrator.py && ruff format --check hivepilot/orchestrator.py` ; `mypy hivepilot/orchestrator.py`

## Agent Notes

**Decisions made:**
- Split the "populate outputs_by_key" logic into a second pure helper, `_stage_outputs_by_key(stage_output, keys) -> dict[str, str]`, layered on top of `_parse_output_sections` (section-extract, then fill any key without a section with the whole `stage_output` blob). Rationale: `_parse_output_sections` is spec'd to return only present sections; keeping the coarse-fallback merge as a second small pure function (rather than inlining the loop body) makes it independently unit-testable (tasks b/c) without mocking the full `run_pipeline`/Orchestrator machinery, and matches the project's "many small, testable functions" style. `run_pipeline`'s stage loop just does `outputs_by_key.update(_stage_outputs_by_key(stage_output, producing_role.outputs))`.
- Producing role resolution mirrors the existing `_agent_name(stage)` pattern exactly: `self.tasks.tasks.get(stage.task)` → `task.role` → `ROLES.get(task.role)` (local `from hivepilot.roles import ROLES` import inside the loop, same as `_agent_name`). Confidence: HIGH — direct precedent in the same file.
- Header matching regex `^##(?!#)\s+(.+?)\s*$` explicitly excludes `### ` (and deeper) sub-headers from being mistaken for a `## ` section boundary or a key match — covered by `test_unrelated_subheader_does_not_match`. Confidence: HIGH (explicit test).
- `outputs_by_key` initialized at :988-area next to `prior_chunks`, updated once per stage right after `stage_output` is computed and right before the existing `prior_chunks.append(...)` line — inserted as pure *addition*, no existing line touched except formatting-driven line wrapping caught by `ruff format`.
- Same-key overwrite ordering (later stage wins) is `dict.update()`'s natural behaviour — no extra logic needed, documented in both the helper's docstring and an inline comment at the call site.

**Assumptions:**
- 🟢 HIGH: `Role.outputs` (read-only, roles.py) is the correct key source per the sprint's shared_contracts; used exactly as declared, never mutated.
- 🟢 HIGH: "byte-identical behaviour" means `prior_chunks` / `build_prior_context` inputs are unaffected — verified via `TestKeyedStoreInertThisSprint.test_prior_context_unchanged_when_role_has_outputs`, which asserts the second stage's `prior_context` still contains the stage-a output's full, unextracted text (including the `## DESIGN_SPEC` header) even though the role declares outputs and a section was found.
- 🟡 MEDIUM: `producing_task.role` may be `None` for tasks with no role configured (common in existing tests using bare `TaskConfig(description=...)` with no `role`); handled via the `producing_task and producing_task.role` guard → `outputs_by_key` simply isn't touched for that stage. No test pipeline in this repo currently exercises a role-less task feeding a role-ful one across the checkpoint, so this path is inferred from the `Role`/`TaskConfig` schema (`role: str | None = None`), not from an existing precedent test.

**Issues found:** none.

**Files needing changes outside boundary:** none encountered — `hivepilot/roles.py` and `hivepilot/config.py` were read-only as declared and were not modified; `hivepilot/cli.py`, `config_provenance.py`, and `docs/tasks/cli/...`/`tasks/` were never touched (outside this sprint's scope per orchestrator instruction).
