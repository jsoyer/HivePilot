# Sprint 4: `model_profiles.yaml` dedup + loader guard

## Meta

- **PRD:** `../spec.md`
- **Sprint:** 4 of 4
- **Depends on:** None
- **Batch:** 1 (parallel with Sprint 1 — fully disjoint files)
- **Model:** sonnet
- **Estimated effort:** S

## Objective

Collapse `model_profiles.yaml` to one source of truth (the repo root, which is the live file) and remove the dead `config/model_profiles.yaml`, with a guard against silent divergence.

## File Boundaries

### Creates (new files)

- `tests/test_model_profiles_single_source.py`

### Modifies (can touch)

- `hivepilot/services/profile_service.py` — add a guard/warning if a stray `config/model_profiles.yaml` reappears and is not the resolved file
- `config/model_profiles.yaml` — **delete this file**

### Read-Only (reference but do NOT modify)

- `hivepilot/config.py` — `claude_profiles_file`:44 (`Path("model_profiles.yaml")`), `resolve_config_path`:235
- `model_profiles.yaml` (root) — the live source; read to confirm content before deleting the dup

### Shared Contracts

- None

### Consumed Invariants (from INVARIANTS.md)

- **`model_profiles.yaml` single source** — after this sprint exactly one file exists (root). Verify: `test -f model_profiles.yaml && ! test -f config/model_profiles.yaml`

## Tasks

- [ ] Diff root `model_profiles.yaml` vs `config/model_profiles.yaml`. If `config/` contains keys/profiles absent from root, MERGE them into the root file first (do not lose data). If `config/` is a strict subset or identical, no merge needed.
- [ ] Delete `config/model_profiles.yaml`.
- [ ] In `profile_service.load_claude_profiles`, after resolving the path, if a `config/model_profiles.yaml` exists on disk but is NOT the resolved path, emit a `rich`/`logging` warning ("stray config/model_profiles.yaml ignored — root is the source of truth") to prevent the silent-masking failure mode.
- [ ] Do NOT change `config.py` `claude_profiles_file` default or `resolve_config_path` — root is already the live file per the resolution chain.

## Acceptance Criteria

- [ ] `config/model_profiles.yaml` no longer exists; root `model_profiles.yaml` remains.
- [ ] `load_claude_profiles()` returns data identical to the pre-change result (assert equal to a snapshot captured before deletion).
- [ ] If a `config/model_profiles.yaml` is re-created in a test, `load_claude_profiles` emits the guard warning and still returns the root-based data.
- [ ] Invariant command `test -f model_profiles.yaml && ! test -f config/model_profiles.yaml` exits 0.

## Verification

- [ ] `pytest tests/test_model_profiles_single_source.py`
- [ ] `python -c "from hivepilot.services.config_validation import validate_config; import sys; sys.exit(1 if validate_config() else 0)"` exits 0
- [ ] Lint passes

## Context

`profile_service.load_claude_profiles(path=None)` resolves via `settings.resolve_config_path(settings.claude_profiles_file)` and reads the top-level `claude_profiles` key (result cached in `_cache`). The XDG chain is XDG_CONFIG_HOME/hivepilot → config_repo → base_dir; `config/model_profiles.yaml` is not on this chain, so it is dead today — deletion is safe. Reset `profile_service._cache` between test assertions if the module caches. This sprint is independent of the CLI-command sprints and touches no shared files with them.

## Agent Notes (filled during execution)

- Assigned to:
- Started:
- Completed:
- Decisions made:
- Assumptions:
- Issues found:
