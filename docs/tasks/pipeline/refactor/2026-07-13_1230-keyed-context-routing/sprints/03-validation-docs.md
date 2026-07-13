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

- [x] In `validate_config`, for each pipeline: walk stages in order; accumulate the set of available output keys (each stage's task→role `outputs`); for each stage, check its role `inputs` against the accumulated set; flag any input not yet produced as a dangling-input problem naming pipeline/stage/input.
- [x] Severity: emit as a WARNING by default; escalate to a hard ERROR when `context_routing_mode` is `keyed` (read the setting via Settings). Ensure the flat problems list / return contract does not break existing `config validate` for configs with cosmetic dangling inputs in `full` mode — i.e. warnings must not gate. (Match how the existing validator distinguishes severities, or introduce a minimal warning channel if none exists — check first.)
- [x] Tests: dangling input flagged (warning in full); a clean config passes; keyed mode escalates the same dangling input to an error; an existing Noxys-style config with cosmetic dangling inputs still passes in full mode.
- [x] Docs (`RUNBOOK.md`, `USAGE.md`): document `inputs`/`outputs` semantics, `context_routing_mode` (full|keyed, default full), the `## <KEY>` section convention, coarse/whole-blob and missing-key fallbacks, and a note that `can_block` is advisory (superseded by stage-level `continue_on_failure`).

## Acceptance Criteria

- [x] Dangling inputs surfaced; existing configs pass in full mode; keyed mode escalates to error.
- [x] Docs cover inputs/outputs, context_routing_mode, section headers, fallbacks, can_block note.

## Verification

- [x] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_config_validation.py` (via `.venv/bin/python`: 6 passed)
- [x] `grep -n context_routing_mode docs/v4/RUNBOOK.md docs/v4/USAGE.md` (4 matches, see Agent Notes)
- [x] Full suite: 949 passed, 2 skipped, 7 pre-existing unrelated failures (tests/test_agent_rules.py, governance file paths under /home/jeromesoyer/Documents/Github/noxys/ that don't exist in this environment). ruff + mypy clean.


## Agent Notes

**Decisions:**
- Severity channel: since `validate_config()` returns a flat `list[str]` with
  no existing severity distinction (confirmed by reading the file first, per
  task instructions), and `hivepilot/cli.py`'s `config validate` command
  treats every list entry as a hard `ERROR` + `typer.Exit(1)`, the least
  invasive option that satisfies "warnings must not gate `full` mode" was:
  dangling-input findings are appended to `problems` (hard error) ONLY when
  `settings.context_routing_mode == "keyed"`; in the default `full` mode they
  are emitted via `warnings.warn(..., UserWarning)` instead, which is the
  same primitive already used elsewhere in this codebase
  (`hivepilot/services/template_service.py`, `hivepilot/cli.py`). This
  required zero changes to `hivepilot/cli.py` (out of file boundaries and
  explicitly off-limits per task instructions) and zero changes to the
  `validate_config()` return type, so every existing caller/test of
  `validate_config()` (incl. `tests/test_init_validate.py`,
  `tests/test_config.py`) keeps working unmodified.
- Verified the ACTUAL bundled Noxys config (roles.yaml/tasks.yaml/pipelines.yaml
  at repo root), not just a synthetic fixture: `validate_config(base_dir=Path("."))`
  returns `problems == []` while emitting 29 dangling-input `UserWarning`s
  across the `company` and `default` pipelines (all cosmetic external inputs
  like `roadmap`, `architecture_docs`, `security_policy`, `existing_docs`).
  `tests/test_init_validate.py::test_validate_current_config_clean` (pre-existing,
  untouched) still passes 5/5, confirming no regression to the real config.
- Accumulation order: a stage's own `outputs` are added to `available_outputs`
  AFTER checking its `inputs` against the running set — so a role can never
  satisfy its own dangling-input check from its own outputs (must come from
  a strictly earlier stage), matching the spec's "not produced by any EARLIER
  stage's outputs" wording.

**Assumptions:**
- 🟢 HIGH confidence: `settings` (module-level `Settings()` singleton) is the
  correct place to read `context_routing_mode` — already imported in
  `config_validation.py` and this exact pattern (`settings.context_routing_mode`)
  is used in `hivepilot/orchestrator.py`.
- 🟢 HIGH confidence: tests should monkeypatch
  `config_validation.settings` (not a fresh `Settings()` instance), matching
  the established pattern across the test suite (e.g.
  `tests/test_pipeline_execution.py` monkeypatches `orchestrator_settings`
  the same way).
- 🟡 MEDIUM confidence: docs were split — RUNBOOK.md (English, ops-facing) got
  the full reference (roles.yaml inputs/outputs, context routing modes,
  `## <KEY>` convention, fallbacks, can_block note, updated "Validate config"
  section); USAGE.md (French, day-to-day CLI/Telegram usage doc) got a
  condensed French section in the same style as its existing "Cibler une
  étape" section, since USAGE.md's existing sections are French-first. If a
  different split was intended, easy to consolidate.

**Issues found:** none — no bugs discovered outside sprint scope.

**Files outside boundary needing changes:** none. `hivepilot/cli.py`'s
`config validate` command was read (read access, not a boundary violation)
to confirm exactly how it consumes `validate_config()`'s return value, per
task instructions ("FIRST read this file to learn its exact structure").
It was NOT modified — per instructions, it is off-limits (another session's
WIP) and, more importantly, the chosen implementation does not require any
CLI change: emitting via `warnings.warn` instead of appending to the flat
`problems` list means the existing `for problem in problems: ... ERROR ...`
loop in `cli.py` needs no changes to correctly treat full-mode dangling
inputs as non-fatal and keyed-mode ones as fatal.

**Environment note:** `python -m pytest` fails in this environment
(`/usr/bin/python: No module named pytest`) — all verification was run via
`.venv/bin/python -m pytest` per the fallback instruction in the task.
