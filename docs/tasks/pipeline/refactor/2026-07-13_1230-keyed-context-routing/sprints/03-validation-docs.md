# Sprint 3: Dangling-input validation + docs

> Self-contained. Load ONLY this file. Part of PRD A2 (keyed context routing).
> Repo: `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot` (Python, pytest, ruff+mypy py3.12).

## Objective

`hivepilot config validate` flags a dangling input (a stage's role `input` not produced by any earlier
stage's role `outputs` in that pipeline). Severity: WARNING by default; ERROR under
`context_routing_mode=keyed`. Document the whole feature.

## Effort: S · Dependencies: Sprint 1, Sprint 2 · Model: sonnet

## Code anchors (verified)

- `hivepilot/services/config_validation.py` — `validate_config(base_dir=...)` returns a problem list; already loads `groups.yaml`/`pipelines.yaml`/`tasks.yaml`/`roles.yaml` and cross-checks refs (incl. the A2-precedent only_tags↔tags check added in a prior PR). Mirror that structure.
- Pipeline stages reference `task`; a task binds to a `role` (tasks.yaml `role:`), and the role has `inputs`/`outputs` (roles.yaml). So the data-flow graph is: stage → task.role → role.inputs/outputs.
- Existing configs have COSMETIC dangling inputs (e.g. developer `inputs:[architecture_docs, codebase_context]` produced by no role) → must NOT break `config validate` in `full` mode.
- Docs: `docs/v4/RUNBOOK.md`, `docs/v4/USAGE.md` document pipeline/stage fields; inputs/outputs semantics are currently UNdocumented.

## File Boundaries

files_to_create:
- (none)

files_to_modify:
- `hivepilot/services/config_validation.py`
- `tests/test_config_validation.py`
- `docs/v4/RUNBOOK.md`
- `docs/v4/USAGE.md`

### Read-Only & Shared Contracts
- read-only: hivepilot/roles.py, hivepilot/config.py
- shared_contracts: dangling-input severity rule (warn in full, error in keyed)

## Tasks

- [ ] In `validate_config`, for each pipeline: walk stages in order; accumulate the set of available output keys (each stage's task→role `outputs`); for each stage, check its role `inputs` against the accumulated set; flag any input not yet produced as a dangling-input problem naming pipeline/stage/input.
- [ ] Severity: emit as a WARNING by default; escalate to a hard ERROR when `context_routing_mode` is `keyed` (read the setting via Settings). Ensure the flat problems list / return contract does not break existing `config validate` for configs with cosmetic dangling inputs in `full` mode — i.e. warnings must not gate. (Match how the existing validator distinguishes severities, or introduce a minimal warning channel if none exists — check first.)
- [ ] Tests: dangling input flagged (warning in full); a clean config passes; keyed mode escalates the same dangling input to an error; an existing Noxys-style config with cosmetic dangling inputs still passes in full mode.
- [ ] Docs (`RUNBOOK.md`, `USAGE.md`): document `inputs`/`outputs` semantics, `context_routing_mode` (full|keyed, default full), the `## <KEY>` section convention, coarse/whole-blob and missing-key fallbacks, and a note that `can_block` is advisory (superseded by stage-level `continue_on_failure`).

## Acceptance Criteria

- [ ] Dangling inputs surfaced; existing configs pass in full mode; keyed mode escalates to error.
- [ ] Docs cover inputs/outputs, context_routing_mode, section headers, fallbacks, can_block note.

## Verification

- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_config_validation.py`
- [ ] `grep -n context_routing_mode docs/v4/RUNBOOK.md docs/v4/USAGE.md`
- [ ] Full suite green; ruff + mypy clean.
