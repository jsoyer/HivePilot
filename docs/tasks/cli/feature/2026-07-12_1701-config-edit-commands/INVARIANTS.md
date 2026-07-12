# INVARIANTS — HivePilot Config Edit Commands

Machine-verifiable contracts for the config-edit-commands feature. The `check-invariants.sh`
PostToolUse hook walks from an edited file up to the project root and runs the `Verify` commands.
All commands are relative to the repo root (`/home/jeromesoyer/orca/workspaces/HivePilot/ideas`).

## Config Cross-References Valid

- **Owner:** `hivepilot/services/config_validation.py` (`validate_config`)
- **Preconditions:** Any command that writes a config file must first run `validate_config` on the *prospective* (post-mutation) state via `config_writer.apply_and_validate`.
- **Postconditions:** After any successful mutation command, `validate_config()` returns `[]` (no cross-ref errors: task→role, pipeline→task, group→project, role→prompt_file all resolve).
- **Invariants:** A mutation that would introduce a new `validate_config` error is never written to disk; the command exits non-zero instead.
- **Verify:** `python -c "from hivepilot.services.config_validation import validate_config; import sys; sys.exit(1 if validate_config() else 0)"`
- **Fix:** Revert the offending edit; re-run the command with a valid reference (or accept the interactive picker at a TTY).

## model_profiles.yaml Single Source

- **Owner:** `hivepilot/services/profile_service.py` (`load_claude_profiles`)
- **Preconditions:** Model profiles are read only through `load_claude_profiles`, which resolves via `settings.resolve_config_path`.
- **Postconditions:** Exactly one `model_profiles.yaml` exists, at the repo root; `config/model_profiles.yaml` does not exist.
- **Invariants:** The root file is the sole source of truth for `claude_profiles`; any stray `config/model_profiles.yaml` is ignored with a warning, never silently merged.
- **Verify:** `test -f model_profiles.yaml && ! test -f config/model_profiles.yaml`
- **Fix:** Merge any unique profiles from the stray file into root `model_profiles.yaml`, then delete the stray file.

## No Secret Echo in config get/list

- **Owner:** `hivepilot/services/config_provenance.py` (+ CLI `config get`/`config list`)
- **Preconditions:** Callers pass a settings key; secret-typed fields (name matches token/secret/password/api_key, or a known credential setting) are flagged.
- **Postconditions:** Secret-typed settings render as `REDACTED` in all output; raw secret values are never printed.
- **Invariants:** `config get`/`config list` output contains no raw secret value under any input.
- **Verify:** `grep -qn "REDACT" hivepilot/services/config_provenance.py`
- **Fix:** Add the missing field pattern to the redaction allowlist in `config_provenance.py`.

## Writes Go Through Round-Trip Helper

- **Owner:** `hivepilot/services/config_writer.py`
- **Preconditions:** All config mutation commands call `config_writer` helpers (`load_roundtrip`/`dump_roundtrip`/`apply_and_validate`).
- **Postconditions:** YAML comments and key order are preserved across edits; unrelated entries are byte-identical except the intended change.
- **Invariants:** No mutation path uses `yaml.safe_dump` (which drops comments/order).
- **Verify:** `! grep -rn "safe_dump" hivepilot/services/config_writer.py`
- **Fix:** Replace any `yaml.safe_dump` with `config_writer.dump_roundtrip` (ruamel round-trip).
