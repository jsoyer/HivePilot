# Sprint 1: Config-writer core + interactivity helper

## Meta

- **PRD:** `../spec.md`
- **Sprint:** 1 of 4
- **Depends on:** None
- **Batch:** 1 (parallel with Sprint 4 — different files)
- **Model:** sonnet
- **Estimated effort:** M

## Objective

Provide the round-trip write + prospective-validate + TTY-aware prompt primitives that Sprints 2 and 3 consume.

## File Boundaries

### Creates (new files)

- `hivepilot/services/config_writer.py`
- `tests/test_config_writer.py`

### Modifies (can touch)

- `pyproject.toml` — add `ruamel.yaml` to dependencies
- `requirements.txt` — add `ruamel.yaml`

### Read-Only (reference but do NOT modify)

- `hivepilot/services/config_validation.py` — `validate_config(base_dir: Path | None = None) -> list[str]` at `:29`; reuse as the write gate
- `hivepilot/services/project_service.py` — `_read_yaml`:11, `load_projects`:18, `load_tasks`:23, `load_pipelines`:28, `load_groups`:33; reference for reading shapes
- `hivepilot/roles.py` — `Role`:32, `_DEFAULT_ROLES`:58, `load_roles`:178; reference for role reference checks
- `hivepilot/config.py` — `Settings`:25, `resolve_config_path`:235; for base_dir resolution
- `hivepilot/services/config_service.py` — `_copy_to_base_dir`:128 / `_copy_from_base_dir`:163; reference for how files are located/written today

### Shared Contracts (produced here, consumed by Sprints 2 & 3)

- `load_roundtrip(path: Path) -> CommentedMap`
- `dump_roundtrip(data, path: Path) -> None`
- `apply_and_validate(file: str, mutate: Callable[[CommentedMap], CommentedMap], *, dry_run: bool, base_dir: Path | None) -> WriteResult` where `WriteResult` has `.diff: str`, `.errors: list[str]`, `.written: bool`
- `resolve_reference(kind: Literal["role","project","task","prompt_file"], value: str) -> bool`
- `prompt_or_refuse(valid: list[str], label: str) -> str | None`

### Consumed Invariants (from INVARIANTS.md)

- **Config cross-references valid** — `apply_and_validate` MUST run `validate_config` on the prospective state and refuse to write when it returns a non-empty list.
- **Writes go through round-trip helper** — no `yaml.safe_dump` in this module; verify: `! grep -rn "safe_dump" hivepilot/services/config_writer.py`

## Tasks

- [x] Add `ruamel.yaml` to `pyproject.toml` `[project].dependencies` and `requirements.txt` (pin a reasonable minimum, e.g. `ruamel.yaml>=0.18`).
- [x] Implement `load_roundtrip` / `dump_roundtrip` using `ruamel.yaml.YAML()` (typ default round-trip; `preserve_quotes=True`).
- [x] Implement `apply_and_validate`: deep-copy the loaded map, apply `mutate`, write the prospective YAML to a temp file inside the same base_dir, run `validate_config(base_dir=<temp base>)` (or validate the mutated in-memory structure through the existing loaders), compute a unified diff vs the original, then write to the real path ONLY if `errors == []` and `not dry_run`. Return `WriteResult`.
- [x] Implement `resolve_reference(kind, value)`: `role`→`value in load_roles()`, `project`→`value in load_projects()`, `task`→`value in load_tasks()`, `prompt_file`→file exists under the prompts dir. Read-only.
- [x] Implement `prompt_or_refuse(valid, label)`: if `sys.stdin.isatty()` use `questionary.select(label, choices=valid).ask()`; else return `None`. Import questionary lazily inside the TTY branch only.
- [x] Define `WriteResult` as a small immutable dataclass (`frozen=True`).

## Acceptance Criteria

- [x] Round-tripping a YAML file that contains comments and a specific key order preserves both; a targeted single-key change shows in a diff with no other lines changed.
- [x] `apply_and_validate` returns `written=False` and non-empty `errors` when the mutation introduces a `validate_config` error, and leaves the original file byte-identical.
- [x] `apply_and_validate(..., dry_run=True)` returns a non-empty `diff` and writes nothing.
- [x] `prompt_or_refuse` returns `None` under a stubbed non-TTY (`sys.stdin.isatty()` monkeypatched to `False`) and does NOT import/call questionary in that path.
- [x] No `yaml.safe_dump` appears in `config_writer.py`.

## Verification

- [x] `pytest tests/test_config_writer.py` — 15 passed / 0 failed (13 original + 2 hardening regression tests)
- [x] `mypy hivepilot/services/config_writer.py` — 0 errors (full `mypy hivepilot`: 0 errors in 77 files)
- [x] Lint passes (`ruff check .` full repo — 0 issues)

## Context

`validate_config()` returns `[]` on success or a list of human-readable problem strings; `_load` (`config_validation.py:18`) raises `ValueError` on YAML parse error — catch and surface it as an error entry, do not let it crash the command. The immutability rule applies: never mutate the caller's loaded map in place — operate on a copy so a failed validation leaves the on-disk (and in-memory) state untouched. Tests follow the existing pattern: stub heavy deps in `sys.modules` if importing `hivepilot.cli` is needed, but this module should be importable without the full CLI.

## Agent Notes (filled during execution)

- Assigned to: sprint-executor (sonnet, isolation:worktree) — branch `worktree-agent-ab783804df4a2aa50`, merged to `jsoyer/ideas` at commit `29ecb05` (feat commit `f282ebb`); hardening follow-up `32c5a46`.
- Started: 2026-07-12 (Batch 1, parallel with Sprint 4)
- Completed: 2026-07-12
- Decisions made:
  - Prospective validation is implemented by copying the six required config files + `prompts/` into a scratch temp dir, overlaying the mutated file, then calling `validate_config(base_dir=scratch)`. Matches the spec's suggested approach.
  - YAML dump indentation set to `mapping=2, sequence=4, offset=2` to match this repo's existing YAML convention (verified vs `roles.yaml`/`projects.yaml`); ruamel's library default (offset=0) would have reformatted every file on dump.
  - `WriteResult.errors` kept as `list[str]` to honor the declared shared contract (a reviewer flagged the shallow mutability; kept the contract as documented since Sprints 2/3 consume `list[str]`).
- Assumptions:
  - `load_roundtrip` raises `FileNotFoundError` for a missing path (mirrors `open()`); `apply_and_validate` handles a missing target gracefully by starting from an empty `CommentedMap()`.
- Issues found / hardening applied post-review (commit `32c5a46`):
  - HIGH: original-file load in `apply_and_validate` could raise an uncaught ruamel `YAMLError` if the on-disk file was already malformed. Now wrapped in try/except → returns `WriteResult(errors=[...], written=False)` instead of crashing (per spec Context: surface parse errors, do not crash).
  - MEDIUM: the write-gate was a no-op for files outside the 6 core config files (e.g. `model_profiles.yaml`, which Sprint 3 will edit) — invalid YAML would have been accepted. Now non-core files get a "mutated text parses cleanly" check before write. No file can be written with unparseable YAML.
  - Two regression tests added: `test_apply_and_validate_malformed_original_file_returns_error_without_raising`, `test_apply_and_validate_non_core_file_rejects_invalid_mutated_content`.
  - Env (outside repo, noted): sprint-executor fixed a cross-repo worktree `check-test-exists.sh` PROJECT_DIR misfire and a stale pydantic `.pyc` on the shared interpreter; `ruamel.yaml` was installed into the main-repo `.venv` for post-merge verification.
  - Open follow-up (non-blocking, for a later batch): the Sprint 4 stray-file guard warns on every `load_claude_profiles` call (bypasses cache) — consider warn-once to avoid log spam.
