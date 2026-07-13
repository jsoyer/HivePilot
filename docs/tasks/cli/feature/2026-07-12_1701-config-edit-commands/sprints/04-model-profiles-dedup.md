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

- [x] Diff root `model_profiles.yaml` vs `config/model_profiles.yaml`. If `config/` contains keys/profiles absent from root, MERGE them into the root file first (do not lose data). If `config/` is a strict subset or identical, no merge needed. — RESULT: schemas were DISJOINT (root: `claude_profiles`/`role_profiles` consumed by loader; config/: `roles`/`fallback` runner-bindings, self-documented as a non-authoritative mirror of `hivepilot/roles.py`, zero code readers). No `claude_profiles` data in config/ to lose → no merge needed. Root left byte-identical.
- [x] Delete `config/model_profiles.yaml`.
- [x] In `profile_service.load_claude_profiles`, after resolving the path, if a `config/model_profiles.yaml` exists on disk but is NOT the resolved path, emit a warning ("stray config/model_profiles.yaml ignored — root is the source of truth").
- [x] Do NOT change `config.py` `claude_profiles_file` default or `resolve_config_path` — left unchanged.

## Acceptance Criteria

- [x] `config/model_profiles.yaml` no longer exists; root `model_profiles.yaml` remains.
- [x] `load_claude_profiles()` returns data identical to the pre-change result (root unchanged; snapshot assertion in test).
- [x] If a `config/model_profiles.yaml` is re-created in a test, `load_claude_profiles` emits the guard warning and still returns the root-based data.
- [x] Invariant command `test -f model_profiles.yaml && ! test -f config/model_profiles.yaml` exits 0.

## Verification

- [x] `pytest tests/test_model_profiles_single_source.py` — 5 passed / 0 failed
- [x] `python -c "... validate_config ..."` exits 0 (INVARIANTS Config-cross-references-valid holds)
- [x] Lint passes (`ruff check .` — 0 issues)

## Context

`profile_service.load_claude_profiles(path=None)` resolves via `settings.resolve_config_path(settings.claude_profiles_file)` and reads the top-level `claude_profiles` key (result cached in `_cache`). The XDG chain is XDG_CONFIG_HOME/hivepilot → config_repo → base_dir; `config/model_profiles.yaml` is not on this chain, so it is dead today — deletion is safe. Reset `profile_service._cache` between test assertions if the module caches. This sprint is independent of the CLI-command sprints and touches no shared files with them.

## Agent Notes (filled during execution)

- Assigned to: sprint-executor (sonnet, isolation:worktree) — branch `worktree-agent-a5496fca0e31c6aa6`, merged to `jsoyer/ideas` at commit `fe7162e` (refactor commit `220f120`).
- Started: 2026-07-12 (Batch 1, parallel with Sprint 1)
- Completed: 2026-07-12
- Decisions made:
  - No merge performed — verified via real diff that `config/model_profiles.yaml` had a disjoint schema (`roles`/`fallback` runner-bindings) from the loader-consumed root (`claude_profiles`). The config/ file's own header declared it a documentary mirror of `hivepilot/roles.py`; `rg` confirmed zero Python readers and it is not on the `resolve_config_path` XDG chain → dead code, safe to delete. Its larger byte size was comments + unrelated doc payload, not profile data.
  - Guard scoped to `resolved.parent / "config" / resolved.name` so it detects a stray sibling copy regardless of which resolution tier supplied the live file.
- Assumptions:
  - `config/model_profiles.yaml` is dead code — verified by grepping all `.py` for the literal path and its unique top-level keys (`roles:`/`fallback:`), and by tracing the XDG→config_repo→base_dir chain.
- Issues found: none. Root `model_profiles.yaml` is byte-for-byte unchanged (`git diff` empty). `validate_config()` returns `[]` post-change.
- Follow-up (non-blocking): guard currently runs on every `load_claude_profiles` call including cache hits — consider warn-once to avoid log spam (flagged by code review; left for a later batch since the passing test asserts the warning fires).
