
# Session Learnings — PRD A1 Stage Scoping & Controls

## Execution Mode: Autonomous (/plan-build-test)

PRD dir: `docs/tasks/pipeline/feature/2026-07-12_1200-stage-scoping-and-controls/`
Build candidate: `build-candidate/stage-scoping-and-controls`. Worktree: `plan-a1-stage-scoping-2-2`.

## Batch status
- **Batch 1 (Sprints 1 + 2): COMPLETE & VERIFIED** — 2026-07-12 session.
- **Batch 2 (Sprint 3: docs + groups.yaml example tags): PENDING** — run `/plan-build-test` in a NEW session; Phase 0 picks up `current_batch: 2` from progress.json.

## ENV (critical — reuse for Batch 2)
- No pre-existing working venv. Created `.venv` via uv. Base `/usr/bin/python` (3.14) & linuxbrew python lack project deps.
- **Full `requirements.txt` does NOT install**: `crewai`→`chromadb`→`chroma-hnswlib==0.7.3` fails to build (`Unsupported compiler -- at least C++11 needed`) under GCC 16 / py3.14. Working install: `VIRTUAL_ENV=.venv uv pip install pytest pytest-asyncio -r <requirements-minus-crewai> -e .`
- **Always run tests via `.venv/bin/python -m pytest -q ...`** (NOT `python`/`python3`). The relevant test set does not transitively need crewai.

## Pre-existing test failures (NOT regressions — confirmed via `git stash` baseline: 762 clean / 783 with changes)
12 failures, all env/config divergence per MEMORY `noxys-config-extraction`: `test_groups.py` (3), `test_default_pipeline.py` (5), `test_orchestrator.py::TestTasksYamlDocumentationBinding` (3) — local groups/pipelines/tasks.yaml are private noxys config, not the acme/default/gemini fixtures those tests expect; `test_artifact_service.py::test_export_s3_without_boto3_raises` (1) — boto3/py3.14 metaclass at import.

## Gotchas hit
- **[TOOLING] `sprint-executor` forces `isolation: worktree`** off MAIN HEAD (`4e30337`, predates PRD docs) — edits land in `.../HivePilot/.claude/worktrees/agent-*`, NOT this orca worktree, and it can't update PRD checkboxes. Mitigation: `git -C <wt> diff HEAD > patch` then `git apply` into orca worktree (source bases were byte-identical). **For Batch 2 prefer `general-purpose` (sonnet) agents run IN-PLACE.**
- **[TOOLING] ruff PostToolUse hook leaks reformat noise** onto out-of-scope files each time agents touch the tree: `hivepilot/services/{api_service,config_validation,db,telegram_bot}.py`, `tests/{test_db_abstraction,test_multi_tenant}.py`. Pure reflows. `git checkout -- <those>` before finalizing; re-check after EVERY agent run.

## Sprint 1 decisions
- Fields per frozen contract; skip = `RunResult(success=True, skipped=True, detail="skipped: ...")` + `continue` before task/prior_chunks/stage_failed. Fail-closed tag validation runs once before the stage loop over all stages.
- **Gap found & fixed (in-scope):** executor added `group_tags` param to `run_pipeline` but left `cli.py` unwired → `only_tags` never resolved at runtime. Fixed: cli.py group-mode passes `group_tags=grp.tags` (cf. MEMORY `role-mapping-not-wired`).
- **Added (danger §2):** `logger.warning("pipeline.failure_suppressed", ...)` when `continue_on_failure` masks a real failure — never silent. CLI shows `⏭️` for skipped (not green ✅). Both regression-tested.

## Sprint 2 notes
- `_resolve_prompt_path(filename, settings)` → `settings.resolve_config_path(prompts/agents/<name>)` with package `_PROMPTS_DIR` final fallback; `.exists()`/`""` guard intact.
- `load_roles()` reads module-level `settings` singleton — tests `monkeypatch.setattr(config.settings, "config_repo", ...)`, not env vars.
- structlog routes through stdlib logging → pytest `caplog.at_level("WARNING")` + substring on `caplog.text` works.

## Rules for Batch 2 (Sprint 3 docs)
1. Doc field names EXACTLY: `only_components`, `only_tags`, `continue_on_failure` (PipelineStage), `tags` (Group); note fail-closed undefined-tag behaviour.
2. Keep diff scoped to `docs/v4/RUNBOOK.md`, `docs/v4/USAGE.md`, `groups.yaml`; `git checkout --` ruff noise after.
3. `groups.yaml` is private noxys data — add an illustrative `tags:` block (`ui: [console, extension, ...]`) without breaking structure or inventing acme fixtures.

---

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
