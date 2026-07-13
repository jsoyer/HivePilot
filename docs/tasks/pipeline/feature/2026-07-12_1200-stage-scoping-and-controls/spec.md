# HivePilot Pipeline Stage Capabilities (A1): Product Requirements Document

> Status: DRAFT — planning only. Execution happens in a fresh session via `/plan-build-test`.
> This is PRD **A1** of a 3-PRD program (A1 → B → A2). A1 is the blocker for **PRD B**
> (Noxys config-repo "designer" role). Keyed inputs/outputs routing is **PRD A2**, out of scope here.

## 1. What & Why

**Problem:** HivePilot pipelines are all-or-nothing per run. Every stage executes on every
target, there is no way to scope a stage to only certain components, and the fail-fast behaviour
cannot be relaxed per stage. This forces near-duplicate pipelines (one per project "type"), which
breaks down under a **monorepo** where a single change can touch UI (`web/console`) *and* backend
(`apps/api`) at once — no single pipeline is correct. Additionally, `Role.prompt_file` is hardcoded
to the installed package directory, so agent prompts cannot be overridden from the config repo.

**Desired Outcome:** A single pipeline can declare, per stage, *which components a stage applies
to* (skipping cleanly when a change does not touch them) and *whether a stage's failure halts the
run*. Agent-request prompts resolve through the same config chain as task prompts. All existing
pipelines behave byte-for-byte identically unless they opt into the new fields.

**Justification:** This is the enabling capability for PRD B (a `designer` stage that runs only on
UI components). It also directly serves the multirepo→monorepo migration: stage scoping lets one
pipeline handle both worlds. Small, bounded, high leverage.

## 2. Correctness Contract

**Audience:** (1) HivePilot maintainer authoring/reading pipelines; (2) the Noxys config repo that
will consume `only_tags: [ui]` and `continue_on_failure: true` on designer stages in PRD B.

**Failure Definition:** Useless if any existing pipeline changes behaviour without opting in
(regression), if a scoped stage runs when it should skip (or skips when it should run), if a skipped
stage is recorded as a failure or leaks into `prior_chunks`, or if `continue_on_failure` does not
actually suppress the fail-fast break.

**Danger Definition:** Harmful if `continue_on_failure` silently lets a *security/review* stage
failure pass unnoticed (masking a real block), if an undefined tag silently resolves to "skip
everything" (a change ships with its intended gate silently bypassed), or if the prompt-resolution
change makes an agent run with an empty/ wrong prompt.

**Risk Tolerance:** A confident-but-wrong skip is worse than a refusal. Prefer **fail-closed**:
misconfiguration (undefined tag, malformed selector) must raise at config-load time, not silently
skip a stage. When ambiguous whether to run a stage, **run it** (default to inclusion).

## 3. Context Loaded

- `hivepilot/models.py`: `PipelineStage` (~95-99) has only `name`, `task`, `pause_before`,
  `commits_vault`. `Group` (~111-117) has only `description`, `hub`, `components: list[str]`.
  Pydantic v2 defaults to `extra="ignore"` → unknown YAML keys are silently dropped, so new fields
  must be real model fields.
- `hivepilot/orchestrator.py`: stage loop spans ~927-1005. `selected_components: list[str]` is
  initialised at :940 and narrowed by `_parse_components(...)` at :946-949 — **already in scope**
  inside the `for stage_idx, stage in enumerate(pipeline.stages)` loop. Fail-fast at :1183-1192:
  `stage_failed = any(not r.success ...)` then `if stage_failed and not getattr(stage,
  "continue_on_failure", False): ... break`. The `getattr` hook is **latent/dead** because the
  field does not exist — always False today.
- `hivepilot/roles.py`: `_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "agents"` (:27);
  joined at :211 inside `load_roles()` (193-213). Used only for inter-agent request messaging
  (orchestrator.py ~587-588, ~746-748), guarded by `.exists()` with `""` fallback.
- `hivepilot/config.py`: `resolve_config_path` (235-255) implements the 3-tier chain
  `xdg_config_home` → `_config_repo_local_path()` (config_repo) → `resolve_path` (base_dir, defaults
  to `Path.cwd()`), each `.exists()`-checked; resolves **arbitrary relative subpaths** already used
  by task-step prompts.
- Status: `RunStatus(str, Enum)` lives in `hivepilot/services/state_service.py:32-55` (NEW, PLANNED,
  RUNNING, PAUSED, REVIEW, APPROVAL, COMPLETE, RATE_LIMIT, AUTH_EXPIRED, TEST_FAILURE — no literal
  SUCCESS; success is COMPLETE). This is **run-level**, not stage-level — a skipped *stage* needs a
  stage-level representation, not a new RunStatus member.
- Tests: flat `tests/` dir, pytest, `asyncio_mode = "auto"`. Relevant: `test_pipeline_execution.py`,
  `test_company_pipeline.py`, `test_group_pipeline.py`, `test_component_selection.py`,
  `test_models.py`, `test_roles.py`, `test_config.py`. Run: `python -m pytest -q`.
- Docs: `docs/v4/RUNBOOK.md` and `docs/v4/USAGE.md` document `PipelineStage` fields and
  `groups.yaml`. `commits_vault` is currently undocumented. `CLAUDE.md` + `AGENTS.md` at repo root.

## 4. Success Metrics

| Metric | Current | Target | How to Measure |
| ------ | ------- | ------ | -------------- |
| Existing pipeline regressions | n/a | 0 | Full `python -m pytest -q` green, pre/post diff of behaviour tests |
| Selector correctness | none | 100% | New unit tests: skip / no-skip / no-selector / tag-resolve all pass |
| `continue_on_failure` correctness | dead hook | works | Tests: flag=true suppresses break; flag=false/absent preserves fail-fast |
| Prompt override reach | package-only | config chain | Test: `Role.prompt_file` resolved from a temp config_repo dir |
| Misconfig fail-closed | silent drop | raises | Test: undefined `only_tags` value raises at load, not at run |

## 5. User Stories

```
GIVEN a pipeline stage with `only_tags: [ui]` and groups.yaml tags {ui: [console, extension]}
WHEN a run's change touches only `api`
THEN the stage is skipped (task not invoked), recorded as skipped, prior_chunks unchanged, run continues

GIVEN the same stage
WHEN the change touches `console`
THEN the stage runs normally

GIVEN a stage with `continue_on_failure: true`
WHEN its task returns success=False
THEN the run does NOT break; subsequent stages still execute

GIVEN a stage with no selector and no continue_on_failure
WHEN it runs
THEN behaviour is identical to today (runs always; failure fail-fast breaks)

GIVEN a role whose prompt_file is overridden in the config repo
WHEN an inter-agent request message is built
THEN the config-repo prompt is used (not the package copy)

GIVEN a pipeline referencing `only_tags: [nonexistent]`
WHEN config is loaded
THEN a clear error is raised at load time (fail-closed)
```

## 6. Acceptance Criteria

- [ ] `PipelineStage` gains `only_components: list[str] | None = None`, `only_tags: list[str] | None = None`, `continue_on_failure: bool = False`; all optional, safe defaults.
- [ ] `Group` gains `tags: dict[str, list[str]] = {}` mapping tag → component names.
- [ ] A stage with a selector is skipped iff the intersection of its resolved target components with `selected_components` is empty; skip does not run the task, does not mark failure, does not append to `prior_chunks`, and the run continues.
- [ ] `only_tags` resolves to component names via the run's `Group.tags`; `only_components` matches names directly; if both are set, their union is the target set.
- [ ] A stage with neither selector always runs (backward compatible).
- [ ] `continue_on_failure: true` suppresses the fail-fast `break`; `false`/absent preserves current fail-fast (`any(not r.success) → break`).
- [ ] `Role.prompt_file` is resolved via `Settings.resolve_config_path()`, preserving the existing `.exists()`/empty-string safety.
- [ ] An `only_tags` value not present in `Group.tags` raises a clear error at config-load time.
- [ ] A skipped stage is distinguishable in the run record from both success and failure.
- [ ] Docs (`docs/v4/RUNBOOK.md`, `docs/v4/USAGE.md`) document the three new `PipelineStage` fields and `groups.yaml` `tags`; the example `groups.yaml` shows a `tags` block.
- [ ] `python -m pytest -q` is fully green.

## 7. Non-Goals (and why)

- **Keyed inputs/outputs / context routing (PRD A2).** Big, risky refactor of the data-flow core; explicitly deferred. `inputs`/`outputs`/`can_block` stay declarative here.
- **Path-glob selectors.** No structure holding *touched file paths* exists today (only component *names* via `selected_components`). Glob matching would need new plumbing — out of scope; component-name/tag matching covers PRD B.
- **Making `can_block` functional.** Blocking is better expressed per-stage via `continue_on_failure` (position-dependent) than per-role. `can_block` stays advisory; documenting/deprecating it is optional and not required here.
- **The Noxys `designer` role/task/pipeline edits.** Those live in the config repo (PRD B).
- **Removing/renaming existing `PipelineStage` fields.** Additive only.

## 8. Technical Constraints

- Stack: Python, Pydantic v2, pytest (`asyncio_mode="auto"`). Package `hivepilot/`.
- Architecture: additive optional fields; reuse the in-scope `selected_components` at orchestrator.py:940-949; reuse `resolve_config_path`. No new dependencies. Fail-closed on misconfig.
- Performance: negligible; skip check is a set intersection per stage.

## 9. Architecture Decisions

| Decision | Reversal Cost | Alternatives Considered | Rationale |
|----------|--------------|-------------------------|-----------|
| Both `only_components` (names) and `only_tags` (sugar) on the stage | Low | names-only (repetitive), tags-only (needs group always) | User chose "both"; names = base mechanism, tags = reuse-once abstraction |
| Selector matches component **names** via `selected_components` | Med | file-path globs | Only component names exist in scope today; globs need new plumbing (A2-ish) |
| Skip = stage-level skip marker, **not** a new `RunStatus` | Low | add `SKIPPED` to `RunStatus` | `RunStatus` is run-level; a skipped stage is a per-stage outcome |
| Undefined tag → **raise at load** (fail-closed) | Low | silent skip | Silent skip could bypass an intended gate — danger per §2 |
| `continue_on_failure` as stage field activating the latent getattr | Low | per-role `can_block` | Blocking is position-dependent; field already half-wired at :1184 |
| `Role.prompt_file` via `resolve_config_path` | Low | leave hardcoded | Consistency with task prompts; enables config-repo override (PRD B) |

## 10. Security Boundaries

- **Auth model:** none introduced; internal orchestration only.
- **Trust boundaries:** pipeline/group YAML is operator-authored (trusted), but a misconfigured selector is a safety risk → validated at load (fail-closed) so a security/review stage is never silently skipped.
- **Data sensitivity:** none new. Prompt files may contain org-specific strategy (handled by PRD B's repatriation); this PRD only changes *resolution*, not storage.
- **Tenant isolation:** n/a.

## 11. Data Model

No datastore. Schema changes are Pydantic models only:

- `PipelineStage`: `+ only_components: list[str] | None = None`, `+ only_tags: list[str] | None = None`, `+ continue_on_failure: bool = False`.
- `Group`: `+ tags: dict[str, list[str]] = {}`.

**Access pattern:** at run time, for each stage, resolve target components = `set(only_components or []) ∪ {c for t in (only_tags or []) for c in group.tags[t]}`; skip if that set is non-empty AND disjoint from `selected_components`.

## 12. Shared Contracts

- **New `PipelineStage` fields** — consumed by PRD B (`only_tags: [ui]`, `continue_on_failure: true`). Names and semantics are frozen here and MUST NOT change in B.
- **`groups.yaml` `tags` block** — `tags: { ui: [console, extension, vscode, website, agent] }` (illustrative; B owns the actual Noxys values).
- **Skip semantics** — skipped stage: task not invoked, no failure, `prior_chunks` untouched, run continues.

## 13. Architecture Invariant Registry

| Concept | Owner | Format/Values | Verify Command |
| ------- | ----- | ------------- | -------------- |
| Backward-compat (no opt-in → unchanged) | orchestrator | existing pipeline tests green | `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py tests/test_company_pipeline.py tests/test_component_selection.py` |
| New stage fields optional w/ defaults | models.py | `only_components/only_tags=None`, `continue_on_failure=False` | `cd .../HivePilot && python -c "from hivepilot.models import PipelineStage; s=PipelineStage(name='x',task='t'); assert s.only_components is None and s.only_tags is None and s.continue_on_failure is False"` |
| Group.tags present & typed | models.py | `dict[str,list[str]]` default `{}` | `cd .../HivePilot && python -c "from hivepilot.models import Group; g=Group(description='d',hub='h',components=[]); assert g.tags=={}"` |
| Undefined tag fails closed | config load | raises on unknown `only_tags` | new test `test_pipeline_execution.py::test_undefined_tag_raises` exits 0 |

**Dependency direction:** PRD B depends on the field names/semantics owned here.

## 14. Open Questions

- [ ] Exact stage-level skip representation (a `skipped` flag on the stage-result object vs a sentinel result). Sprint 1 decides; contract in §12 must hold regardless.
- [ ] Where undefined-tag validation fires — at `Config`/`Settings` load vs at pipeline-resolve. Prefer earliest point that has both pipeline stages and group tags in hand. Sprint 1 decides.
- [ ] Whether inter-agent-request prompt lookup should log at DEBUG when falling back to `""` (nice-to-have, not required).

## 15. Uncertainty Policy

- When uncertain whether a stage should run: **run it** (default to inclusion).
- On misconfiguration (undefined tag, malformed selector): **stop/raise at load** (fail-closed).
- When backward-compat conflicts with a cleaner design: prefer **backward-compat**.

## 16. Verification

- Deterministic: `python -m pytest -q` fully green, including new tests for skip / no-skip / no-selector / tag-resolution / union-of-selectors / continue_on_failure true+false / Role.prompt_file config-chain resolution / undefined-tag-raises. The four INVARIANTS.md `Verify` commands exit 0.
- Manual: reviewer confirms the orchestrator skip branch leaves `prior_chunks` untouched and does not increment failure counters; confirms docs match field names.

## 17. Sprint Decomposition

### Sprint Overview

| Sprint | Title | Depends On | Batch | Model | Parallel With |
| ------ | ----- | ---------- | ----- | ------ | ------------- |
| 1 | Stage scoping + continue_on_failure + skip | None | 1 | sonnet | Sprint 2 |
| 2 | Role.prompt_file resolution via config chain | None | 1 | sonnet | Sprint 1 |
| 3 | Docs + groups.yaml example tags | 1, 2 | 2 | sonnet | — |

Sprints 1 and 2 touch disjoint files (models.py/orchestrator.py/state_service.py vs roles.py) and
disjoint test files → parallel-safe under worktree isolation. Sprint 3 documents both, so it depends
on 1 and 2.

### Sprint 1: Stage scoping + continue_on_failure + skip → `sprints/01-stage-scoping.md`

**Objective:** Add the three `PipelineStage` fields + `Group.tags`, implement the skip logic and
`continue_on_failure` activation, with fail-closed tag validation and full tests.
**Estimated effort:** M
**Dependencies:** None

**File Boundaries:**
- `files_to_create`: (none — extend existing test files)
- `files_to_modify`: `hivepilot/models.py`, `hivepilot/orchestrator.py`, `tests/test_pipeline_execution.py`, `tests/test_models.py` (add cases); possibly `tests/test_component_selection.py`
- `files_read_only`: `hivepilot/services/state_service.py`, `hivepilot/config.py`, root `groups.yaml`, `pipelines.yaml`
- `shared_contracts`: `PipelineStage` fields + skip semantics (§12)

**Tasks:**
- [ ] Add `only_components`, `only_tags`, `continue_on_failure` to `PipelineStage`; add `tags` to `Group`.
- [ ] In the stage loop (~927-1005), compute target components (union of names + tag-resolved), skip when non-empty ∧ disjoint from `selected_components`; record a stage-level skip that is neither success nor failure and leaves `prior_chunks` untouched.
- [ ] Validate `only_tags` against `Group.tags` and raise a clear error on unknown tag (fail-closed).
- [ ] Simplify/keep the `getattr(stage, "continue_on_failure", False)` line at :1184 now that the field exists; verify fail-fast preserved when false/absent.
- [ ] Unit tests: skip-excludes, no-skip-matches, no-selector-runs, only_components, only_tags, union, undefined-tag-raises, continue_on_failure true+false.

**Acceptance Criteria:**
- [ ] All §6 criteria except the two prompt-resolution ones and the docs one.

**Verification:**
- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_pipeline_execution.py tests/test_models.py tests/test_component_selection.py`
- [ ] The first three INVARIANTS.md verify commands exit 0.

### Sprint 2: Role.prompt_file resolution → `sprints/02-role-prompt-resolution.md`

**Objective:** Resolve `Role.prompt_file` through `Settings.resolve_config_path()` so config-repo
prompt overrides are picked up by inter-agent request messaging, preserving `.exists()` safety.
**Estimated effort:** S
**Dependencies:** None

**File Boundaries:**
- `files_to_create`: (none)
- `files_to_modify`: `hivepilot/roles.py`, `tests/test_roles.py`
- `files_read_only`: `hivepilot/config.py`, `hivepilot/orchestrator.py` (call sites ~587, ~746)
- `shared_contracts`: none

**Tasks:**
- [ ] Change the `_PROMPTS_DIR / prompt_filename` join (roles.py:211) so resolution goes through `resolve_config_path` (XDG → config_repo → cwd), keeping the package dir as the final fallback and the `.exists()`/`""` guard at the call sites.
- [ ] Test: a prompt placed in a temp config_repo dir is resolved over the package copy.

**Acceptance Criteria:**
- [ ] `Role.prompt_file` resolves via the config chain; `.exists()`/empty-string safety intact.

**Verification:**
- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -m pytest -q tests/test_roles.py`

### Sprint 3: Docs + example tags → `sprints/03-docs.md`

**Objective:** Document the new fields and `groups.yaml` `tags`.
**Estimated effort:** S
**Dependencies:** Sprint 1, Sprint 2

**File Boundaries:**
- `files_to_create`: (none)
- `files_to_modify`: `docs/v4/RUNBOOK.md`, `docs/v4/USAGE.md`, root `groups.yaml` (add an illustrative `tags` block)
- `files_read_only`: `hivepilot/models.py` (final field names)
- `shared_contracts`: field names from §12

**Tasks:**
- [ ] Document `only_components`, `only_tags`, `continue_on_failure` on `PipelineStage` and `tags` on groups; note the fail-closed undefined-tag behaviour. (Optionally document the pre-existing undocumented `commits_vault`.)
- [ ] Add an illustrative `tags:` block to the example `groups.yaml`.

**Acceptance Criteria:**
- [ ] Docs describe all three new stage fields + group tags with an example; names match the model exactly.

**Verification:**
- [ ] Manual doc review; `grep -n "only_tags\|only_components\|continue_on_failure" docs/v4/RUNBOOK.md docs/v4/USAGE.md` returns hits.

## 18. Execution Log

[Filled during execution — tracked in progress.json]

## 19. Learnings (filled after all sprints complete)

[Compound step output]
