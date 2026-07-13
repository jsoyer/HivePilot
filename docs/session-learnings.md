
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


## config-edit-commands — Batch 1 complete (2026-07-12)

- **PRD:** docs/tasks/cli/feature/2026-07-12_1701-config-edit-commands/ (Build Candidate `build-candidate/config-edit-commands`)
- **Done:** Sprint 1 (config-writer-core) + Sprint 4 (model-profiles-dedup). progress.json updated: 1,4 `complete`; 2,3 `not_started`.
- **Shipped:** `hivepilot/services/config_writer.py` (round-trip ruamel writer + prospective-validation write gate + TTY-aware prompt helpers), profile_service stray-file guard, deleted dead `config/model_profiles.yaml`, +`ruamel.yaml>=0.18`. No CLI wiring yet — internal primitives only.
- **Verify:** ruff PASS, mypy PASS (77 files), pytest 781 passed / 11 pre-existing FAIL (local-config divergence — test_default_pipeline/test_groups/test_orchestrator, untouched by this batch; do NOT chase under this PRD) / 5 skipped. New tests 20/20 green.
- **Contracts for Sprint 2/3:** `apply_and_validate(file, mutate, *, dry_run, base_dir) -> WriteResult(.diff/.errors/.written)`; `resolve_reference(kind, value) -> bool`; `prompt_or_refuse(valid, label) -> str | None` (None in headless — refuse, never proceed). Write gate now rejects unparseable YAML for ANY file.
- **NEXT:** New session → `/plan-build-test` picks up Batch 2 (Sprint 2 config-get-list, deps [1] satisfied).


## A2 keyed-context-routing — COMPLETE (2026-07-13)

- **PRD:** docs/tasks/pipeline/refactor/2026-07-13_1230-keyed-context-routing/ (build-candidate/keyed-context-routing). All 3 sprints done + committed on branch `plan/a2-keyed-context-routing`; NOT pushed/merged. progress.json overall status=complete.
- **Shipped:** `Settings.context_routing_mode` (full|keyed, default full); `_route_prior_context()` + `_parse_output_sections`/`_stage_outputs_by_key` in orchestrator.py; dangling-input check in config_validation.py (warn in full / hard-error in keyed); docs in RUNBOOK/USAGE. Feature ships DORMANT — full mode byte-identical.
- **Verify:** all 5 INVARIANTS.md checks PASS; full suite 949 passed / 7 pre-existing unrelated FAIL (tests/test_agent_rules.py — noxys governance paths absent locally; do NOT chase) / 2 skipped. ruff + mypy clean. Code reviews PASS (gating on flag only, own-outputs-after-input-check ordering, cap semantics correct).
- **Commit history is redundant:** two commits per sprint (subagent's real-code commit + my near-empty progress.json bump with a misleading full-sprint message). Functionally correct, no dup symbols. `git rebase -i` unavailable in this env; left as-is. `/ship-test-ensure` should squash.

### LESSONS (CONFIG/PROCESS)
- **[RESUME] Check the working-tree diff on resume, not just progress.json + git log.** progress.json said all 3 sprints `pending`, but the entire PRD was already implemented uncommitted in a prior interrupted session (only Sprint 1 committed). `git status --short` + `git diff --stat` at Phase 0 is mandatory — reconcile drift before planning/executing, or you'll re-implement done work.
- **[DELEGATION] general-purpose verification subagents SHARE the main working tree and WILL `git commit`/edit files even when told "verification only"** (they have full tools + Bash). Two subagents committed their sprints (cde82c4, a4ded4c) despite explicit "do NOT modify" instructions. Fix: for read-only verification use the `Explore` agent (no Edit/Write/commit), OR explicitly forbid `git add`/`git commit` AND file edits in the prompt, OR run verification myself. Never assume a full-tool subagent is read-only just because the prompt says so.

