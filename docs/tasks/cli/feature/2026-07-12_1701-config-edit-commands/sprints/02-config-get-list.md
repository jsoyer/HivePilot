# Sprint 2: `config get` / `config list` + provenance

## Meta

- **PRD:** `../spec.md`
- **Sprint:** 2 of 4
- **Depends on:** Sprint 1
- **Batch:** 2 (parallel with Sprint 3 — different files; both only touch `cli.py` in additive, non-overlapping command registrations, but to be safe run after Sprint 1 and coordinate `cli.py` edits — see conflict note)
- **Model:** sonnet
- **Estimated effort:** M

> **cli.py conflict note:** Sprints 2 and 3 both modify `hivepilot/cli.py`. They must NOT run in the same parallel batch. Orchestrator: run Sprint 2, merge, then Sprint 3 (or vice versa). They are sequenced, not parallel, despite both being "batch 2" conceptually. `progress.json` encodes this via `depends_on`.

## Objective

Add read-only introspection: `config get <key>` and `config list` show resolved setting values with their provenance (source file + XDG rank), redacting secrets.

## File Boundaries

### Creates (new files)

- `hivepilot/services/config_provenance.py`
- `tests/test_cli_config_get.py`

### Modifies (can touch)

- `hivepilot/cli.py` — add `@config_app.command("get")` and `@config_app.command("list")` (config_app declared at `:33`, registered `:34`)

### Read-Only (reference but do NOT modify)

- `hivepilot/config.py` — `Settings`:25, `resolve_config_path`:235, `xdg_config_home` prop `:214`, `_config_repo_local_path`:226, `resolve_path`:223, `claude_profiles_file`:44
- `hivepilot/services/config_writer.py` — reuse helpers if needed (do not modify)

### Shared Contracts (produced here)

- `Provenance` dataclass: `{ value, source_path: Path | None, xdg_rank: int, redacted: bool }` (rank 1=XDG, 2=config_repo, 3=base_dir, 0=default/no-file)

### Consumed Invariants (from INVARIANTS.md)

- **No secret echo in config get/list** — secret-typed fields (names containing token/secret/password/key, and `.env`-backed credentials) MUST render as `REDACTED`. Verify: output of `config list` never contains a raw token value in tests.

## Tasks

- [ ] Implement `resolve_with_provenance(key: str) -> Provenance`: read `getattr(settings, key)`; for path-backed settings, walk `resolve_config_path` order to determine which file (XDG→config_repo→base_dir) actually provides it and set `xdg_rank`.
- [ ] Implement secret detection: a field is secret if its name matches a redaction allowlist (`token`, `secret`, `password`, `api_key`, etc.) or is one of the known credential settings; such fields set `redacted=True` and `value="REDACTED"`.
- [ ] Wire `config get <key>`: print value + source_path + rank; unknown key → exit 1 with the list of valid keys.
- [ ] Wire `config list`: Rich table with columns `key | value(or REDACTED) | source | rank`, iterating `Settings.model_fields`.

## Acceptance Criteria

- [ ] `config get <key>` prints the resolved value plus its source file and XDG rank.
- [ ] A setting overridden via a file under `$XDG_CONFIG_HOME/hivepilot/` reports rank 1 (XDG) as its source.
- [ ] Secret-typed fields display `REDACTED`, never the raw value, in both `get` and `list`.
- [ ] `config list` includes every `Settings` field.
- [ ] `config get <unknown-key>` exits non-zero and lists valid keys.

## Verification

- [ ] `pytest tests/test_cli_config_get.py`
- [ ] `mypy hivepilot/services/config_provenance.py`
- [ ] Lint passes

## Context

`Settings` is pydantic `BaseSettings` (env_prefix `HIVEPILOT_`). Non-path settings have no file provenance → rank 0, source_path `None`, source label "default/env". Only path-typed / file-backed settings (e.g. `claude_profiles_file`) get a real file resolution via `resolve_config_path`. Tests: monkeypatch `$XDG_CONFIG_HOME` to a tmp dir containing a `hivepilot/<file>` to assert rank 1. Use Typer `CliRunner`; stub heavy deps in `sys.modules` before `import hivepilot.cli` per the existing test pattern in `tests/test_cli.py`.

## Agent Notes (filled during execution)

- Assigned to:
- Started:
- Completed:
- Decisions made:
- Assumptions:
- Issues found:
