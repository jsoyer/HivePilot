# Sprint 3: Guided mutations — `project add/rm`, `task set-role`, `role wire`

## Meta

- **PRD:** `../spec.md`
- **Sprint:** 3 of 4
- **Depends on:** Sprint 1 (config_writer), Sprint 2 (serializes `cli.py` edits)
- **Batch:** 3 (sequential — must not run in parallel with Sprint 2 because both modify `cli.py`)
- **Model:** sonnet
- **Estimated effort:** L

## Objective

Add the validated, idempotent, dry-run-able edit commands: `project add/rm`, `task set-role`, `role wire` — each refuses to write a broken cross-reference.

## File Boundaries

### Creates (new files)

- `tests/test_cli_config_commands.py`

### Modifies (can touch)

- `hivepilot/cli.py` — add `project` / `task` / `role` sub-apps (or top-level commands) following the `x_app = typer.Typer(...)` + `app.add_typer(x_app, name=...)` pattern (`:22-46`). **Run after Sprint 2 has merged its `cli.py` changes.**

### Read-Only (reference but do NOT modify)

- `hivepilot/services/config_writer.py` — `apply_and_validate`, `resolve_reference`, `prompt_or_refuse`, `WriteResult`
- `hivepilot/services/project_service.py` — `load_projects`:18, `load_tasks`:23; for current entries
- `hivepilot/roles.py` — `load_roles`:178, `Role`:32; for valid role names + wireable fields
- `hivepilot/services/config_validation.py` — `validate_config`:29 (used via `apply_and_validate`)

### Shared Contracts (consumed from Sprint 1)

- `apply_and_validate(file, mutate, *, dry_run, base_dir) -> WriteResult`
- `resolve_reference(kind, value) -> bool`
- `prompt_or_refuse(valid, label) -> str | None`

### Consumed Invariants (from INVARIANTS.md)

- **Config cross-references valid** — every mutation goes through `apply_and_validate`; a command that would introduce a `validate_config` error MUST exit non-zero and write nothing.
- **Writes go through round-trip helper** — commands never call `yaml.safe_dump` directly; verify: `! grep -rn "safe_dump" hivepilot/cli.py` for the new command bodies.

## Tasks

- [ ] `project add <name> <path>`: idempotent (existing identical entry = no-op message), `--dry-run`, round-trip write to `projects.yaml`.
- [ ] `project rm <name>`: remove entry; missing name → exit 1 with valid names; `--dry-run`.
- [ ] `task set-role <task> <role>`: if `role` not in `load_roles()`, at TTY call `prompt_or_refuse(valid_roles, ...)` to pick; in non-TTY exit 1 + print valid roles. On valid role, write via `apply_and_validate`.
- [ ] `role wire <role> <field> <value>`: accept **ANY** field of the `Role` model (decision 2026-07-13: full-field editing, not an allowlist). Coerce `<value>` to the field's declared type: str fields as-is; `order` → int; `can_block` → bool; `models`/`inputs`/`outputs` → list[str] (comma-split); `permission_mode` → validate against the allowed enum values. For reference-bearing fields (`prompt_file`, `runner`, `model`, `model_profile`) additionally verify the target exists via `resolve_reference`. Unknown field name → exit 1 listing valid `Role` fields; a value that fails type-coercion or enum/reference validation → exit 1 with guidance and writes nothing.
- [ ] Shared options across commands: `--dry-run` and `--no-input` (force non-interactive refuse even at TTY). Exit codes: 0 = written/no-op, 1 = refused/invalid.
- [ ] For a task/role that does not exist as the *subject* of the command (e.g. `set-role` on an unknown task), exit 1 with valid subjects.

## Acceptance Criteria

- [ ] Each command writes only when the prospective config validates clean (zero new `validate_config` errors).
- [ ] Invalid reference: non-TTY (or `--no-input`) exits 1 and lists valid values; TTY shows the questionary picker.
- [ ] Re-running any command with identical args is a no-op (idempotent) and exits 0.
- [ ] `--dry-run` prints a diff and writes nothing.
- [ ] Comments and key order in the edited YAML are preserved after a real write.
- [ ] `role wire` accepts any valid `Role` field, coerces the value to the field's type, and rejects (exit 1, no write) an unknown field name, a value that won't coerce, an invalid `permission_mode` enum, or a dangling reference.

## Verification

- [ ] `pytest tests/test_cli_config_commands.py`
- [ ] Test asserts `validate_config()` returns `[]` after each successful command.
- [ ] Lint + `mypy` pass on changed `cli.py` sections.

## Context

Reference-bearing role fields per `Role` (`roles.py:32`): confirm exact field names by reading the model at execution time; only wire fields that point at another config entity or a file. Non-TTY detection is provided by Sprint 1's `prompt_or_refuse` (returns `None`); `--no-input` should force that same `None` path even when a TTY is present. Tests use Typer `CliRunner` with `input=` for the interactive branch and a monkeypatched non-TTY for the refuse branch, following `tests/test_cli.py` / `tests/test_prompt_cli_runner.py` patterns (stub heavy deps in `sys.modules` before `import hivepilot.cli`).

## Agent Notes (filled during execution)

- Assigned to:
- Started:
- Completed:
- Decisions made:
- Assumptions:
- Issues found:
