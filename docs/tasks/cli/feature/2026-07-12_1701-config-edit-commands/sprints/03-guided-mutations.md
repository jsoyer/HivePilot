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

- [x] `project add <name> <path>`: idempotent (existing identical entry = no-op message), `--dry-run`, round-trip write to `projects.yaml`.
- [x] `project rm <name>`: remove entry; missing name → exit 1 with valid names; `--dry-run`.
- [x] `task set-role <task> <role>`: if `role` not in `load_roles()`, at TTY call `prompt_or_refuse(valid_roles, ...)` to pick; in non-TTY exit 1 + print valid roles. On valid role, write via `apply_and_validate`.
- [x] `role wire <role> <field> <value>`: accept **ANY** field of the `Role` model (decision 2026-07-13: full-field editing, not an allowlist). Coerce `<value>` to the field's declared type: str fields as-is; `order` → int; `can_block` → bool; `models`/`inputs`/`outputs` → list[str] (comma-split); `permission_mode` → validate against the allowed enum values. For reference-bearing fields (`prompt_file`, `runner`, `model`, `model_profile`) additionally verify the target exists via `resolve_reference`. Unknown field name → exit 1 listing valid `Role` fields; a value that fails type-coercion or enum/reference validation → exit 1 with guidance and writes nothing.
- [x] Shared options across commands: `--dry-run` and `--no-input` (force non-interactive refuse even at TTY). Exit codes: 0 = written/no-op, 1 = refused/invalid.
- [x] For a task/role that does not exist as the *subject* of the command (e.g. `set-role` on an unknown task), exit 1 with valid subjects.

## Acceptance Criteria

- [x] Each command writes only when the prospective config validates clean (zero new `validate_config` errors).
- [x] Invalid reference: non-TTY (or `--no-input`) exits 1 and lists valid values; TTY shows the questionary picker.
- [x] Re-running any command with identical args is a no-op (idempotent) and exits 0.
- [x] `--dry-run` prints a diff and writes nothing.
- [x] Comments and key order in the edited YAML are preserved after a real write.
- [x] `role wire` accepts any valid `Role` field, coerces the value to the field's type, and rejects (exit 1, no write) an unknown field name, a value that won't coerce, an invalid `permission_mode` enum, or a dangling reference.

## Verification

- [x] `pytest tests/test_cli_config_commands.py`
- [x] Test asserts `validate_config()` returns `[]` after each successful command.
- [x] Lint + `mypy` pass on changed `cli.py` sections.

## Context

Reference-bearing role fields per `Role` (`roles.py:32`): confirm exact field names by reading the model at execution time; only wire fields that point at another config entity or a file. Non-TTY detection is provided by Sprint 1's `prompt_or_refuse` (returns `None`); `--no-input` should force that same `None` path even when a TTY is present. Tests use Typer `CliRunner` with `input=` for the interactive branch and a monkeypatched non-TTY for the refuse branch, following `tests/test_cli.py` / `tests/test_prompt_cli_runner.py` patterns (stub heavy deps in `sys.modules` before `import hivepilot.cli`).

## Agent Notes (filled during execution)

- Assigned to: sprint-executor (sonnet), worktree `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot-cfgedit` on branch `feat/config-edit-cli`
- Started/Completed: 2026-07-13 (single session)

### Decisions made

1. **No `--config-dir` flag.** All three commands call `apply_and_validate(..., base_dir=None)`, relying on `settings.resolve_config_path`'s existing XDG → config_repo → base_dir chain — the same chain every other CLI command already uses. Tests isolate via `monkeypatch.setattr(settings, "base_dir", tmp_path)` + an empty `XDG_CONFIG_HOME` override (mirrors `tests/test_cli_config_get.py`).
2. **Idempotency comparisons use the raw round-trip map, not the pydantic model.** Added a small private helper `_load_raw_config_file()` that loads the real on-disk file via `config_writer.load_roundtrip()` and compares the *raw* YAML values (e.g. `roles.yaml`'s `order` as a plain int) against the candidate. Comparing against `load_projects()`/`load_roles()` instead would have given false negatives — `ProjectConfig.path` is expanded/resolved to an absolute path by a `model_validator`, so a literal string comparison against a freshly-typed CLI arg would never match even when the entry is unchanged.
3. **`project add` is a full-entry replace, not a merge.** The flags describe the *whole* desired projects.yaml entry; re-running with the same args is a no-op, but running `project add name new-path` on an existing entry that also had `--description`/`--owner-repo` set previously, without repeating those flags, will drop them. This matches the "declarative" style of the other guided commands (`task set-role` / `role wire` only touch a single field and are therefore safe from this footgun; `project add` is not, since `ProjectConfig` doesn't cleanly decompose into a single addressable field). Flagged as a known limitation below.
4. **TTY-interactive picker only wired for `task set-role`.** Re-reading the sprint spec's per-task bullets (not just the acceptance-criteria summary), only `task set-role`'s invalid-role case explicitly calls for `prompt_or_refuse`. `role wire`'s task bullet lists straightforward `exit 1` for every failure mode (unknown field/role, bad coercion, bad enum, dangling reference) with no TTY picker mentioned. `project add`/`project rm` have no ambiguous reference to resolve at all. All four commands still accept `--no-input` for CLI parity/forward-compatibility, but it is only load-bearing in `task set-role`.
5. **`--no-input`/`--dry-run` present on every command** even where a given command has no TTY branch (`project add/rm`, `role wire`), for consistent UX and to satisfy "shared options across commands" — unused in those bodies beyond passing `dry_run` through to `apply_and_validate`.

### Assumptions

- 🟢 **`permission_mode` enum** = `{acceptEdits, bypassPermissions, plan, default}`, sourced directly from `hivepilot/config.py:56-62`'s doc-comment (`claude_permission_mode`) and corroborated by `claude_runner.py`'s `_ELEVATED_PERMISSION_MODES = {bypassPermissions, acceptEdits}` (the other two, `plan`/`default`, are the non-elevated modes named in the same comment).
- 🟢 **`runner` field reference-check** validates against `get_args(hivepilot.models.RunnerKind)` (the same Literal already used by `RunnerDefinition.kind`) — every existing `roles.yaml` `runner:` value (`opencode`, `cursor`, `claude`, `codex`, `gemini`) is a member of that Literal.
- 🟡 **`model_profile` field reference-check** validates against `profile_service.load_claude_profiles()` keys (`model_profiles.yaml`'s `claude_profiles:` section) rather than `config_writer.resolve_reference` (whose `ReferenceKind` Literal only covers `role`/`project`/`task`/`prompt_file`, not `model_profile`). `profile_service` isn't in this sprint's read-only reference list but is an existing, unmodified service already used by `claude_runner.py` for the same lookup — imported, not touched.
- 🟡 **`model` field has NO reference-existence check.** The sprint spec lists `model` among "reference-bearing fields ... verify the target exists via resolve_reference", but `resolve_reference`'s `ReferenceKind` Literal (in the read-only `config_writer.py`) has no `model` kind, and there is no registry of valid model-version strings anywhere in the codebase (values like `gpt-5.5`, `opencode-go/glm-5.2` are opaque, provider-specific). Implemented as a plain string field with no additional validation. This is the one place I deviated from the letter of the spec; documented here rather than inventing a fake enum or extending the read-only `config_writer.py` (out of file-boundary scope for a "must not modify" file).

### Issues found / deviations

- **`model` field reference check not implemented** (see assumption above) — `role wire <role> model <value>` accepts any non-empty string. If a real model registry is added later (e.g. via a models.yaml), this should gain the same existence check as `runner`/`model_profile`.
- **`project add` overwrite footgun** (see decision 3) — no `--merge` option exists yet to update a single project field without restating the whole entry. Out of this sprint's scope; flagging for a possible follow-up `project set-field` command analogous to `role wire`.
- No files outside the declared boundaries needed modification. `hivepilot/services/profile_service.py` and `hivepilot/models.py` (`RunnerKind`) were *imported*, not modified.
