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

- [ ] Add `_parse_output_sections(text: str, keys: list[str]) -> dict[str, str]` mirroring `_parse_components`: for each key, find a `## <HEADER>` section matching the key (decide+document the mapping — e.g. case-insensitive match of the key with `_`/space/`-` normalization, so `design_spec` matches `## DESIGN_SPEC`), return `{key: section_body}` for those present.
- [ ] In the stage loop, after `stage_output` is computed, populate a run-scoped `outputs_by_key: dict[str, str]`: for the producing stage's role, if `_parse_output_sections` finds sections, store those; for any declared output key without a section, store the whole `stage_output` blob (coarse fallback). Later stages overwrite same-key entries (document this).
- [ ] Do NOT consume `outputs_by_key` anywhere yet; the `prior_chunks`/`build_prior_context` path is unchanged → behaviour identical.
- [ ] Tests in `tests/test_pipeline_execution.py`: `## KEY` section extracted to the right key; coarse whole-blob fallback when no section; a role with multiple outputs and no sections maps the blob to each key; case-insensitive/normalized header match (`design_spec` ↔ `## DESIGN_SPEC`).

## Acceptance Criteria

- [ ] Store populated per the section/coarse rules; no consumption yet.
- [ ] Existing behaviour byte-identical (full suite green — the store is inert).

## Verification

- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py`
- [ ] Full suite: `python -m pytest -q` ; `ruff check hivepilot/orchestrator.py && ruff format --check hivepilot/orchestrator.py` ; `mypy hivepilot/orchestrator.py`
