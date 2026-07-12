# HivePilot Config Edit Commands: Product Requirements Document

## 1. What & Why

**Problem:** HivePilot's CLI (`hivepilot/cli.py`, ~1900 lines of Typer) only *scaffolds* and *validates-after-the-fact*. Every config change means hand-editing YAML across ~9 files (`projects.yaml`, `tasks.yaml`, `roles.yaml`, `pipelines.yaml`, `groups.yaml`, …). Cross-references (task→role, pipeline→task, group→project, role→prompt_file) are only caught *later* by `validate_config()` — never at edit time. This is the direct cause of the `role-mapping-not-wired` class of bugs: a task can silently point at a non-existent role, and `roles.yaml` parse errors silently fall back to `_DEFAULT_ROLES`, masking mistakes. Separately, `model_profiles.yaml` exists in two places (root + `config/`) with no single source of truth. There is also no way to introspect *which* resolved config value is live and *where* it comes from in the XDG precedence chain.

**Desired Outcome:** A small set of surgical, edit-time-validated commands added to the existing CLI that (a) make guided, safe edits that refuse to write a broken cross-reference, (b) let a user introspect resolved config values with their provenance, and (c) collapse `model_profiles.yaml` to one source of truth.

**Justification:** `role-mapping-not-wired` is a logged production blocker. The friction of manual YAML editing plus after-the-fact validation is the root cause. `questionary` (interactive prompts) is already a dependency and already used in `orchestrator.py`; the loaders and validators already exist. This is high-leverage plumbing on top of infrastructure that is already there.

## 2. Correctness Contract

**Audience:** HivePilot operators / config maintainers editing project, role, task, pipeline and group wiring — both interactively (a human at a TTY) and non-interactively (CI, scripts, other agents shelling out to `hivepilot`).

**Failure Definition:** The feature is useless if it (a) lets a broken cross-reference reach disk (regressing `role-mapping-not-wired`), (b) destroys a user's hand-authored YAML — losing comments, key order, or unrelated entries — on write, or (c) reports the wrong provenance/value for `config get`, misleading the user about which file is actually live.

**Danger Definition:** Harmful if it silently overwrites or corrupts existing config files, if the interactive prompt hangs or writes an unintended value in a non-TTY/CI context, or if the `model_profiles.yaml` dedup deletes the *live* file and breaks profile loading.

**Risk Tolerance:** A refusal (exit non-zero, nothing written, tell the user why) is strongly preferred over a confidently-wrong write. For config mutation, a false "I refused / I couldn't" is safe; a false "done" that wrote garbage is catastrophic. Bias every ambiguous case toward no-write + clear message.

## 3. Context Loaded

- `hivepilot/cli.py:22` — top-level `app = typer.Typer(...)`; `:33` `config_app = typer.Typer(help="Config repo sync")`, registered `:34` `app.add_typer(config_app, name="config")`. Sub-commands via `@config_app.command("name")` (e.g. `config_sync`:823, `config_push`:842, `config_status`:857, `config_log`:870). Sub-app registration pattern: `x_app = typer.Typer(...)` then `app.add_typer(x_app, name=...)`.
- `hivepilot/services/project_service.py` — `_read_yaml`:11 → pydantic model_validate; **read-only** loaders: `load_projects()`:18 (`ProjectsFile`), `load_tasks()`:23, `load_pipelines()`:28, `load_groups()`:33. No existing write path for these.
- `hivepilot/roles.py` — `class Role(BaseModel)`:32, `_DEFAULT_ROLES`:58, `load_roles() -> dict[str, Role]`:178 (read-only, **silently falls back to defaults on error** — the masking bug).
- `hivepilot/services/config_service.py` — reads+writes ALL config files for repo sync: `_copy_to_base_dir`:128, `_copy_from_base_dir`:163. Reference for how writes/copies are done today.
- `hivepilot/services/config_validation.py:29` — `validate_config(base_dir: Path | None = None) -> list[str]` (empty list = OK; `_load`:18 raises `ValueError` on YAML parse error). This is the cross-ref validator to reuse.
- `hivepilot/services/lint_service.py:17` — `lint_configuration() -> List[str]`.
- `hivepilot/services/profile_service.py` — `load_claude_profiles(path: Path | None = None)`; resolves via `settings.resolve_config_path(settings.claude_profiles_file)`. `config.py:44` `claude_profiles_file = Path("model_profiles.yaml")`. **Root `model_profiles.yaml` is the live file; `config/model_profiles.yaml` is NOT in the resolution chain → dead.**
- `hivepilot/config.py:235` — `resolve_config_path(self, filename) -> Path`. XDG precedence: (1) `$XDG_CONFIG_HOME/hivepilot/<name>` (prop `:214`) if exists → (2) `_config_repo_local_path()/<name>` (`:226`, only if `config_repo` is an existing local dir) if exists → (3) `base_dir/<name>` (`resolve_path`, `:223`). Settings are pydantic `BaseSettings` fields on `class Settings`(`:25`); a `config get` reads `getattr(settings, key)`.
- Deps: `questionary` — **already** a dependency (`pyproject.toml:21`) and used (`orchestrator.py:11`, `.select`/`.text`/`.confirm`). `ruamel.yaml` — **NOT yet** a dependency (code uses stdlib `yaml.safe_load`); needed for round-trip writes.
- Tests: `tests/test_cli.py`, `tests/test_prompt_cli_runner.py`, `tests/test_groups.py`, `tests/test_role_runner_binding.py`. Pattern: stub heavy deps in `sys.modules` before `import hivepilot.cli`, then Typer `CliRunner`.

## 4. Success Metrics

| Metric | Current | Target | How to Measure |
| ------ | ------- | ------ | -------------- |
| Broken cross-ref reaching disk via a config edit | Possible (manual editing, no gate) | Impossible via new commands | Test: `task set-role X <bad-role>` writes nothing, exits non-zero (non-TTY) |
| Comments/key-order preserved on edit | N/A (no edit cmds) | 100% preserved | Test: round-trip a commented YAML, diff shows only intended change |
| `model_profiles.yaml` sources of truth | 2 (root + `config/`) | 1 (root) | `test -f config/model_profiles.yaml` returns false; profiles still load |
| Provenance visible for a resolved setting | 0 commands | `config get`/`list` show file + XDG rank | Test: `config get` output names the resolving file |
| New commands covered by tests | — | ≥ 80% of new lines | coverage on new modules |

## 5. User Stories

GIVEN a `tasks.yaml` where task `deploy` needs a role
WHEN the operator runs `hivepilot task set-role deploy developer`
THEN the role reference is validated against `roles.yaml`, written to `tasks.yaml` preserving comments/order, and confirmed.

GIVEN the operator typos a non-existent role at an interactive terminal
WHEN they run `hivepilot task set-role deploy dveloper`
THEN questionary lists the valid roles to pick from instead of writing the typo.

GIVEN the same command runs in CI (non-TTY)
WHEN the role is invalid
THEN the command refuses (exit non-zero), writes nothing, and prints the valid roles — it does NOT hang on a prompt.

GIVEN a config spread across the XDG chain
WHEN the operator runs `hivepilot config get claude_profiles_file` (or `config list`)
THEN they see the resolved value AND which file/XDG rank it came from.

GIVEN `model_profiles.yaml` duplicated at root and `config/`
WHEN the dedup ships
THEN `config/model_profiles.yaml` is gone, the root remains the single source, and profile loading is unchanged.

## 6. Acceptance Criteria

- [ ] `hivepilot project add <name> <path>` and `project rm <name>` create/remove entries in `projects.yaml`, idempotently, preserving comments/order.
- [ ] `hivepilot task set-role <task> <role>` sets a task's role, validating the role exists in `roles.yaml` before writing.
- [ ] `hivepilot role wire <role> <field> <value>` wires a role reference (e.g. `prompt_file`) with existence validation of the referenced target.
- [ ] Every mutating command runs `validate_config()` on the *prospective* result and refuses to write if new cross-ref errors are introduced.
- [ ] In a TTY, an invalid reference triggers a `questionary` picker of valid values; in a non-TTY/CI context the same case exits non-zero with the valid values printed (no prompt, no hang).
- [ ] Every mutating command supports `--dry-run` showing the diff without writing, and is idempotent (re-running with same args = no-op).
- [ ] Writes preserve YAML comments and key order (round-trip), leaving unrelated entries byte-identical except the intended change.
- [ ] `hivepilot config get <key>` prints the resolved value and its provenance (file path + XDG rank); `hivepilot config list` dumps all resolved settings with provenance.
- [ ] `config/model_profiles.yaml` is deleted; root `model_profiles.yaml` remains the single source; `load_claude_profiles()` still returns the same data.
- [ ] All new commands have tests (CliRunner) covering: happy path, invalid-ref refusal (non-TTY), idempotence, and `--dry-run`. `validate_config()` reports zero errors on the repo's own config after changes.

## 7. Non-Goals (at least as detailed as goals)

- **No generic `config set foo.bar=x`.** Rejected: it just relocates manual YAML editing into a more verbose syntax without solving cross-ref safety — the actual pain. Only *guided, validated* mutations (`project add`, `task set-role`, `role wire`) are in scope.
- **No TUI / interactive dashboard.** Rejected: over-engineering for the current user count; questionary pickers on failure are sufficient.
- **No changes to `config.py` Settings defaults or `resolve_config_path` for the dedup.** The root file is already the live one; touching the resolution core is high reversal-cost and risks the `config_repo` sync semantics. Dedup is a pure file deletion + a loader guard.
- **No rewrite of the existing untyped loaders into pydantic** beyond what the mutation path needs. `project_service` already uses pydantic models; roles already validate. We do not refactor `policy_service`/`schedule_service`/`token_service`.
- **No `config sync/push/status/log` changes.** The existing `config_service` repo-sync commands stay as-is; new commands are additive.
- **No new mutation commands for `pipelines.yaml`/`groups.yaml`/`policies.yaml` beyond validation of references to them.** Scope is projects, tasks, roles. Pipelines/groups mutation can follow later if the pattern proves out.

## 8. Technical Constraints

- **Stack:** Python ≥3.10, Typer + Rich CLI, pydantic v2 / pydantic-settings, `questionary` (already present), `ruamel.yaml` (NEW dep — required for comment/order-preserving round-trip writes; user-approved).
- **Architecture:** Add commands to the *existing* `hivepilot/cli.py` via the established sub-app pattern. Put write logic in a new `services/config_writer.py` (round-trip read/modify/write + prospective-validate), NOT inline in the CLI. Reuse `config_validation.validate_config()` for the gate and `project_service`/`roles` loaders for reading. Follow the immutability rule: build the new mapping, validate, then write.
- **Performance:** N/A (interactive CLI, sub-second).
- **Interactivity contract:** Detect TTY via `sys.stdin.isatty()` (or Typer/Click context). Interactive fix ONLY when a TTY is attached AND not disabled by a `--no-input`/`--yes` style flag; otherwise refuse with exit code 1. Never call `questionary` in a non-TTY path.

## 9. Architecture Decisions

| Decision | Reversal Cost | Alternatives Considered | Rationale |
|----------|--------------|------------------------|-----------|
| Guided validated mutations only (no generic `config set`) | Low | Generic get/set of arbitrary keys | Generic set doesn't solve cross-ref safety, the actual pain |
| `ruamel.yaml` for round-trip writes | Med | Stay on `yaml.safe_load`+`yaml.safe_dump` (loses comments/order); regex-patch YAML (fragile) | Preserving hand-authored comments/order is an explicit correctness requirement |
| Prospective-validate before write, refuse on new cross-ref errors | Low | Write-then-validate (status quo); warn-and-write | Directly kills `role-mapping-not-wired`; refusal is the safe failure mode |
| Interactive fix only at TTY, hard-refuse in CI | Low | Always prompt (hangs CI); always refuse (worse UX for humans) | Serves both human and script/agent callers safely |
| Dedup by deleting dead `config/model_profiles.yaml`, keep root | Low | Repoint Settings to `config/` and delete root | Root is already the live file; touching resolution core is high-risk for zero gain |
| Write logic in new `services/config_writer.py` | Low | Inline in `cli.py` (already ~1900 lines) | Testability + keeps CLI thin; matches many-small-files rule |

## 10. Security Boundaries

- **Auth model:** None — local CLI operating on local files owned by the invoking user. No network, no new endpoints.
- **Trust boundaries:** Command args (`<name>`, `<path>`, `<role>`, `<value>`) are user-controlled. Validate/normalize before writing: reject path traversal outside expected config dirs, reject keys/values that aren't valid YAML scalars, and confirm referenced targets exist. Never `eval`/template user input into YAML.
- **Data sensitivity:** `api_tokens.yaml` / `.env` hold secrets — new commands MUST NOT touch or print secret files. `config get`/`list` must redact secret-typed settings (token/password fields) rather than echo them. Scope mutation strictly to projects/tasks/roles YAML.
- **Tenant isolation:** N/A (single-user local tool).

## 11. Data Model

No schema changes. Existing pydantic models are reused: `ProjectsFile` (`project_service.py`), `Role` (`roles.py:32`), and the task/pipeline/group shapes read by `project_service`. Writers must round-trip through these validators (read → model_validate for checking, but write via ruamel to preserve formatting).

**Access Patterns:** (1) mutate one entry in one YAML file, re-validate whole config, write once; (2) read one resolved setting + its source file (provenance); (3) dump all resolved settings + sources.

## 12. Shared Contracts

- **`config_writer` interface (Sprint 1, consumed by Sprint 2 & 3):**
  - `load_roundtrip(path: Path) -> CommentedMap` — read preserving comments/order.
  - `dump_roundtrip(data, path: Path) -> None` — write preserving comments/order.
  - `apply_and_validate(file: str, mutate: Callable[[CommentedMap], CommentedMap], *, dry_run: bool, base_dir: Path | None) -> WriteResult` — apply mutation on an in-memory copy, run `validate_config` on the prospective state, return a `WriteResult{diff, errors, written: bool}`; write only if `errors == []` and not `dry_run`.
  - `resolve_reference(kind: Literal["role","project","task","prompt_file"], value: str) -> bool` — existence check used by the interactive/refuse gate.
- **Interactivity helper (Sprint 1):** `prompt_or_refuse(valid: list[str], label: str) -> str | None` — questionary picker at TTY, else `None` (caller refuses with exit 1).
- **Provenance type (Sprint 2):** `Provenance{value, source_path: Path, xdg_rank: int, redacted: bool}`.

## 13. Architecture Invariant Registry

| Concept | Owner | Format/Values | Verify Command |
| ------- | ----- | ------------- | -------------- |
| Config cross-references valid | `services/config_validation.py` | `validate_config()` returns `[]` | `python -c "from hivepilot.services.config_validation import validate_config; import sys; sys.exit(1 if validate_config() else 0)"` |
| `model_profiles.yaml` single source | `services/profile_service.py` | exactly one file, at repo root | `test -f model_profiles.yaml && ! test -f config/model_profiles.yaml` |
| No secret echo in config get/list | `services/config_writer.py` / CLI | token/password fields redacted | `grep -n "redact" hivepilot/services/config_writer.py` |
| Writes go through round-trip helper | `services/config_writer.py` | no `yaml.safe_dump` in new mutation commands | `! grep -rn "safe_dump" hivepilot/services/config_writer.py` |

**Dependency direction:** `cli.py` (commands) depends on `config_writer` and `config_validation`; those depend on `config.py` Settings and the `project_service`/`roles` loaders. No reverse edges.

## 14. Open Questions

- [ ] Should `role wire` support all `Role` fields or only reference-bearing ones (`prompt_file`, `runner`, `model`)? Default assumption: only reference-bearing + a documented allowlist; non-reference scalar fields raise "use manual edit or a later `role set`". (Owner: reviewer during Sprint 3.)
- [ ] For `config list`, do we dump only "interesting" settings or all ~100 `Settings` fields? Assumption: all fields, secrets redacted, grouped. (Owner: reviewer.)

## 15. Uncertainty Policy

- When uncertain whether a mutation is safe: **Stop** (refuse, exit non-zero, explain). Never guess-and-write.
- When uncertain about a value in an *interactive* TTY session: **prompt** the user with valid options.
- When uncertain about a value in a *non-interactive* session: **refuse**, print valid options, exit 1.
- When "developer UX convenience" conflicts with "no broken cross-ref on disk": prefer **correctness** (refuse) over convenience.
- When "preserve existing formatting" conflicts with "make the intended change": make the change, preserve everything else; if formatting can't be preserved, surface a warning rather than silently reflowing the file.

## 16. Verification

- **Deterministic:**
  - `pytest tests/` (new: `tests/test_config_writer.py`, `tests/test_cli_config_commands.py`) — happy path, invalid-ref refusal in non-TTY, idempotence, `--dry-run` diff-no-write, round-trip comment preservation.
  - `validate_config()` returns `[]` on repo config after each command in tests.
  - Invariant verify commands (section 13) exit 0.
  - `ruff`/`mypy` clean on new modules; `hivepilot doctor` still passes.
- **Manual:** Reviewer confirms: an invalid `task set-role` at a real terminal shows the questionary picker; the same in a piped/CI invocation exits 1 without hanging; `config get` on a setting overridden via `$XDG_CONFIG_HOME` reports the XDG source; a commented `projects.yaml` keeps its comments after `project add`.

## 17. Sprint Decomposition

Maximum 5 sprints. Specs extracted to `sprints/`; progress in `progress.json`.

### Sprint Overview

| Sprint | Title | Depends On | Batch | Model | Parallel With |
| ------ | ----- | ---------- | ----- | ------ | ------------- |
| 1 | Config-writer core + interactivity helper | None | 1 | sonnet | — |
| 2 | `config get` / `config list` + provenance + secret redaction | Sprint 1 | 2 | sonnet | Sprint 3 |
| 3 | Guided mutations: `project add/rm`, `task set-role`, `role wire` | Sprint 1 | 2 | sonnet | Sprint 2 |
| 4 | `model_profiles.yaml` dedup + loader guard | None | 1 | sonnet | Sprint 1 |

### Sprint 1: Config-writer core + interactivity helper → `sprints/01-config-writer-core.md`

**Objective:** Provide the round-trip write + prospective-validate + TTY-aware prompt primitives all other sprints consume.
**Estimated effort:** M
**Dependencies:** None

**File Boundaries:**
- `files_to_create`: `hivepilot/services/config_writer.py`, `tests/test_config_writer.py`
- `files_to_modify`: `pyproject.toml`, `requirements.txt` (add `ruamel.yaml`)
- `files_read_only`: `hivepilot/services/config_validation.py`, `hivepilot/services/project_service.py`, `hivepilot/roles.py`, `hivepilot/config.py`, `hivepilot/services/config_service.py`
- `shared_contracts`: `config_writer` interface + `prompt_or_refuse` (section 12)

**Tasks:**
- [ ] Add `ruamel.yaml` to `pyproject.toml` + `requirements.txt`.
- [ ] Implement `load_roundtrip`/`dump_roundtrip` (comment/order preserving).
- [ ] Implement `apply_and_validate(...) -> WriteResult{diff, errors, written}` (mutate copy → `validate_config` on prospective state → write only if clean & not dry-run).
- [ ] Implement `resolve_reference(kind, value)` existence checks against loaders.
- [ ] Implement `prompt_or_refuse(valid, label)` — questionary at TTY, `None` otherwise (via `sys.stdin.isatty()`).

**Acceptance Criteria:**
- [ ] Round-trip of a commented YAML preserves comments/order (diff shows only intended change).
- [ ] `apply_and_validate` refuses (no write) when the mutation introduces a `validate_config` error; writes when clean.
- [ ] `--dry-run` path returns a diff and writes nothing.
- [ ] `prompt_or_refuse` returns `None` under a stubbed non-TTY and never imports questionary in that path.

**Verification:**
- [ ] `pytest tests/test_config_writer.py`
- [ ] `mypy hivepilot/services/config_writer.py`

### Sprint 2: `config get` / `config list` + provenance → `sprints/02-config-get-list.md`

**Objective:** Read-only introspection of resolved settings with provenance and secret redaction.
**Estimated effort:** M
**Dependencies:** Sprint 1 (for shared helpers/type; read-only use)

**File Boundaries:**
- `files_to_create`: `hivepilot/services/config_provenance.py`, `tests/test_cli_config_get.py`
- `files_to_modify`: `hivepilot/cli.py` (add `@config_app.command("get")`, `@config_app.command("list")`)
- `files_read_only`: `hivepilot/config.py`, `hivepilot/services/config_writer.py`
- `shared_contracts`: `Provenance` type (section 12)

**Tasks:**
- [ ] `resolve_with_provenance(key) -> Provenance` using `Settings` + `resolve_config_path` XDG chain (rank 1 XDG / 2 config_repo / 3 base_dir).
- [ ] Redact secret-typed fields (token/password/`.env`-backed) in output.
- [ ] Wire `config get <key>` and `config list` (Rich table: key, value|REDACTED, source, rank).

**Acceptance Criteria:**
- [ ] `config get <key>` prints value + source file + XDG rank.
- [ ] A setting overridden via `$XDG_CONFIG_HOME` reports XDG as source.
- [ ] Secret fields show `REDACTED`, never the raw value.
- [ ] `config list` dumps all settings with provenance.

**Verification:**
- [ ] `pytest tests/test_cli_config_get.py`
- [ ] Manual: override a key via env, confirm provenance.

### Sprint 3: Guided mutations → `sprints/03-guided-mutations.md`

**Objective:** `project add/rm`, `task set-role`, `role wire` — validated, idempotent, dry-run-able edits.
**Estimated effort:** L
**Dependencies:** Sprint 1

**File Boundaries:**
- `files_to_create`: `tests/test_cli_config_commands.py`
- `files_to_modify`: `hivepilot/cli.py` (new `project`/`task`/`role` sub-apps or commands)
- `files_read_only`: `hivepilot/services/config_writer.py`, `hivepilot/services/project_service.py`, `hivepilot/roles.py`, `hivepilot/services/config_validation.py`
- `shared_contracts`: `config_writer` interface + `prompt_or_refuse`

**Tasks:**
- [ ] `project add <name> <path>` / `project rm <name>` — idempotent, `--dry-run`, round-trip write.
- [ ] `task set-role <task> <role>` — validate role exists; TTY→picker, CI→refuse.
- [ ] `role wire <role> <field> <value>` — reference-bearing fields only (allowlist), existence-checked.
- [ ] Shared `--dry-run` / `--no-input` options; consistent exit codes (0 ok, 1 refused/invalid).

**Acceptance Criteria:**
- [ ] Each command writes only when the prospective config validates clean.
- [ ] Invalid ref: non-TTY exits 1 + lists valid values; TTY shows picker.
- [ ] Re-running any command with same args is a no-op (idempotent).
- [ ] `--dry-run` shows diff, writes nothing; comments/order preserved on real writes.

**Verification:**
- [ ] `pytest tests/test_cli_config_commands.py`
- [ ] `validate_config()` returns `[]` after each command in tests.

### Sprint 4: `model_profiles.yaml` dedup → `sprints/04-model-profiles-dedup.md`

**Objective:** One source of truth for model profiles (root), remove the dead `config/` copy safely.
**Estimated effort:** S
**Dependencies:** None (independent; Batch 1 with Sprint 1)

**File Boundaries:**
- `files_to_create`: `tests/test_model_profiles_single_source.py`
- `files_to_modify`: `hivepilot/services/profile_service.py` (add a guard/warn if a stray `config/model_profiles.yaml` reappears), delete `config/model_profiles.yaml`
- `files_read_only`: `hivepilot/config.py`
- `shared_contracts`: none

**Tasks:**
- [ ] Confirm root `model_profiles.yaml` content is the union/correct one; if `config/` had unique keys, merge into root first.
- [ ] Delete `config/model_profiles.yaml`.
- [ ] Add a loader guard: if `config/model_profiles.yaml` exists but isn't the resolved file, emit a warning (masking-prevention).

**Acceptance Criteria:**
- [ ] `config/model_profiles.yaml` no longer exists; root remains.
- [ ] `load_claude_profiles()` returns identical data to pre-change.
- [ ] Invariant `test -f model_profiles.yaml && ! test -f config/model_profiles.yaml` passes.

**Verification:**
- [ ] `pytest tests/test_model_profiles_single_source.py`
- [ ] `hivepilot doctor` / `validate_config()` clean.

## 18. Execution Log

[Filled during execution — tracked in progress.json]

## 19. Learnings (filled after all sprints complete)

[Compound step output]
