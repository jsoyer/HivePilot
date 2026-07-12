# Sprint 2: Role.prompt_file resolution via config chain

> Self-contained. Load ONLY this file. Part of PRD A1.
> Repo: `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot` (Python, pytest `asyncio_mode="auto"`).

## Objective

Resolve `Role.prompt_file` through `Settings.resolve_config_path()` so a prompt override placed in
the config repo is picked up by inter-agent request messaging — instead of the current hardcoded
package-dir lookup. Preserve the existing `.exists()` / empty-string safety.

## Estimated effort: S · Dependencies: None · Model: sonnet · Parallel with: Sprint 1

## Code anchors (verified)

- `hivepilot/roles.py`: `_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "agents"` (:27). Join at :211 inside `load_roles()` (193-213): `entry["prompt_file"] = _PROMPTS_DIR / prompt_filename`.
- Consumers: inter-agent request messaging at `hivepilot/orchestrator.py` ~587-588 and ~746-748, guarded by `.exists()` with `""` fallback.
- `hivepilot/config.py`: `resolve_config_path(name)` (235-255) — 3-tier chain `xdg_config_home` → config_repo (`_config_repo_local_path`) → `base_dir` (`resolve_path`, defaults `Path.cwd()`), each `.exists()`-checked; already used for task-step prompts and resolves arbitrary relative subpaths like `prompts/agents/xxx.md`.

## File Boundaries

files_to_create:
- (none)

files_to_modify:
- `hivepilot/roles.py`
- `tests/test_roles.py`

### Read-Only & Shared Contracts
- read-only: hivepilot/config.py, hivepilot/orchestrator.py (call sites only)
- shared_contracts: none

## Tasks

- [ ] Change the resolution so `prompt_file` goes through `resolve_config_path` (XDG → config_repo → cwd), keeping the package `_PROMPTS_DIR` as the FINAL fallback if the config chain does not find the file. Note: `load_roles()` may need access to a `Settings` instance — thread it in or resolve lazily at the call sites (~587/~746) rather than at load; pick whichever keeps `.exists()`/`""` safety intact and does not break existing role loading.
- [ ] Preserve behaviour when no override exists: the package copy is still found (fallback), and a missing file still yields the `.exists()`→`""` guard, never a crash.
- [ ] Test in `tests/test_roles.py`: with a temp config_repo dir containing `prompts/agents/<role>.md`, the resolved prompt path points to the config-repo copy, not the package copy. Add a test that a missing override still falls back cleanly.

## Acceptance Criteria

- [ ] `Role.prompt_file` resolves via the config chain with package-dir fallback; `.exists()`/empty-string safety intact; existing role tests unchanged and green.

## Verification

- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_roles.py tests/test_roles_yaml.py tests/test_role_runner_binding.py`
- [ ] Full suite green: `python -m pytest -q`
