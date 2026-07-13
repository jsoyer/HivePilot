
## Compact Checkpoint — 2026-06-18T14:00:20Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-18T18:26:45Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-18T20:10:53Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-18T20:31:16Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-19T06:16:05Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-19T09:19:55Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-19T10:00:44Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-19T17:14:08Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-20T08:48:14Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Compact Checkpoint — 2026-06-21T10:25:01Z

- **CWD:** /home/jeromesoyer/Documents/Github/jsoyer/HivePilot
- **Action:** Re-read this file after compaction. Resume from last completed phase.


## Batch 1 — Stage Scoping & Controls (PRD A1) — 2026-07-12

**Status:** Batch 1 COMPLETE (Sprints 1 & 2). Batch 2 (Sprint 3: docs + groups.yaml example tags) PENDING — run `/plan-build-test` again to pick it up.

**Delivered:**
- `PipelineStage` + `only_components`, `only_tags`, `continue_on_failure`; `Group` + `tags: dict[str,list[str]]`.
- Orchestrator: `_validate_stage_tags` (fail-closed ValueError on undefined tag), `_stage_scope_target` (union of names + tag-resolved), stage-level skip via `RunResult.skipped=True` (task not invoked, prior_chunks untouched, run continues), fail-fast simplified to `stage.continue_on_failure`.
- `roles.py`: `Role.prompt_file` resolved via `settings.resolve_config_path(prompts/agents/<file>)` with package `_PROMPTS_DIR` fallback; `.exists()`/"" safety intact.
- **cli.py wiring fix (integration, beyond Sprint 1 boundary):** group-mode `run_pipeline` now passes `group_tags=grp.tags` — without it `only_tags` was decorative/non-functional end-to-end. Small clear fix → applied autonomously per escalation rule.

**Verification (in W1 worktree venv, byte-identical merged files):** full `pytest -q` = 777 passed / 11 failed / 5 skipped, exit 0; the 11 failures are PRE-EXISTING config-drift (stale tasks.yaml/pipelines.yaml/groups.yaml: missing `default` pipeline, `cos-synthesis`/`documentation` tasks, `acme` group), 0 new failures from our changes. ruff + mypy clean on all 4 source files.

**LESSON (ENV):** My orca workspace has NO `.venv` — `python -m pytest` fails with `ModuleNotFoundError: hivepilot`. Sprint executors' worktrees under `Documents/.claude/worktrees/` DO have working venvs. Reused Sprint 1's worktree venv to verify the merged combination, then copied the 7 verified files back. Final-batch Phase 5 will need a workspace venv set up.

**LESSON (worktree-reformat-noise, reconfirmed):** After executors ran, 6 UNRELATED files in the main workspace (`services/api_service.py`, `config_validation.py`, `db.py`, `telegram_bot.py`, `tests/test_db_abstraction.py`, `test_multi_tenant.py`) showed as modified — pure ruff line-joining noise (mtime matched executor run). `git restore`d them so the batch contains only the 7 intended files. Always `git status --short` + restore stray reformat noise before finalizing.

**LESSON (worktree base drift):** `isolation:worktree` executors branched off the OLD root commit `4e30337` (pre-PRD-planning `37b5fc7`), so the sprint spec files didn't exist in their worktrees — they couldn't tick checkboxes. Code files were byte-identical between the two commits, so copying target files back was safe. Orchestrator ticked spec checkboxes in the main workspace afterward.
