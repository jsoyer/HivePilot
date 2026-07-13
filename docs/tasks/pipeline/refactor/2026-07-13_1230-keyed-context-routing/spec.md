# Keyed Inputs/Outputs Context Routing (A2): Product Requirements Document

> Status: DRAFT — planning only. Execution via `/plan-build-test` in a fresh session.
> PRD **A2** of the A1 → B → A2 program. A1 (stage scoping + continue_on_failure) and B
> (Noxys designer) are DONE and merged. A2 is the deep structural item; independent of A1/B.

## 1. What & Why

**Problem:** Every pipeline stage today receives `prior_chunks` — the CONCATENATION of all
previous stages' markdown output — injected into its prompt (`orchestrator.py:988,1145,1128`;
`claude_runner._build_prompt:200-232`). `Role.inputs`/`Role.outputs` (`roles.py:43-44`, `list[str]`)
have **zero functional references** — purely declarative. As pipelines grow (noxys-v2 is 12 stages),
every stage sees everything: token bloat, no data contracts, no relevance routing.

**Desired Outcome:** `inputs`/`outputs` become functional via **keyed context routing** — a stage
consumes only the prior outputs it declares, pulled by key from a store, instead of the whole blob.
The designer's `## DESIGN_SPEC` / `## UI_REVIEW` sections (shipped in B) become precisely routable.

**Justification:** This was explicitly deferred from A1 as "the real structural reinforcement." It is
the foundation for context hygiene, clearer contracts, and future parallelism as pipelines lengthen.

## 2. Correctness Contract

**Audience:** HivePilot maintainer; Noxys pipelines (esp. the designer).

**Failure Definition:** Useless if an existing pipeline's stage stops seeing context it used to
(regression), if a declared input silently resolves to empty content, or if the store mis-keys output.

**Danger Definition:** The central danger — **silently dropping context a stage needs**, producing a
confidently-wrong output. TWO concrete traps: (1) existing `roles.yaml` **already declares `inputs`
on every role** (cosmetically); if routing triggered on input-*presence*, every existing pipeline
would silently switch from full-context to a keyed subset — a mass regression. (2) A dangling input
(declared but never produced upstream) resolving to empty.

**Risk Tolerance:** A confident-but-wrong drop is far worse than showing too much. Prefer
**conservative fallback** (show more) and **explicit opt-in**. Misconfiguration under keyed routing
must **fail closed at validate time**, not silently.

## 3. Context Loaded

- `orchestrator.py:988` `prior_chunks: list[str] = []`; appended `:1145`
  `prior_chunks.append(f"## {agent} ({stage.name})\n{stage_output}")`; consumed `:1128-1132` via
  `build_prior_context(prior_chunks, mode=settings.prior_context_mode, max_chars=...)`.
- `build_prior_context` (`:223-256`) modes `full`/`synthesis`/`cap` (default `cap`, 8000 chars,
  `config.py:123`) — slices by recency/keyword only, **never role-aware**.
- Stage output is a flat `str` (`RunResult.detail`, dataclass `:261-269`); aggregated `:1142-1145`.
  No structured StageResult.
- `Role.inputs`/`Role.outputs` (`roles.py:43-44`) — **zero functional refs** anywhere in `hivepilot/`.
- Section-parse precedent: `_parse_components` (`:157-173`) extracts a `COMPONENTS:` line. Stage
  outputs get `## {Agent} ({stage})` headers but are **never parsed back**. Designer prompts (B) emit
  `## DESIGN_SPEC` / `## UI_REVIEW`.
- **Hook point:** `claude_runner._build_prompt:200-232` — `prior = payload.metadata.get("prior_context")`.
  Flows `run_task(prior_context=...)` → `_execute_task` → `metadata["prior_context"]` (`:1663`).
  A role-scoped slice replaces the blanket string exactly here (and where it's computed, `:1128`).
- Vault store: `write_stage_artifact` (`pipelines.py:38-70`, called `orchestrator.py:1146`) persists
  each stage's markdown keyed by run_id+stage_name — **write-only, never read back**.
- `hivepilot config validate` (`services/config_validation.py`) cross-checks group/pipeline refs and
  (A2's precedent) only_tags↔group tags. Data-flow (input↔output) is NOT checked today.
- Tests: `tests/test_pipeline_execution.py` (prior_chunks propagation, e.g.
  `test_skipped_stage_not_in_prior_chunks:436-478`), `test_company_pipeline.py`, `test_group_pipeline.py`.
  Docs: `docs/v4/` — inputs/outputs semantics **undocumented**.

## 4. Success Metrics

| Metric | Current | Target | How to Measure |
| ------ | ------- | ------ | -------------- |
| Existing pipeline regressions | n/a | 0 | full suite green; prior_context byte-identical in `full` mode |
| Default behaviour changed | — | none | `context_routing_mode` defaults to `full`; opt-in only |
| Keyed routing correctness | none | 100% | tests: consumer gets only declared inputs |
| Section extraction + fallback | none | works | tests: `## KEY` section pulled; whole-blob fallback when absent |
| Context reduction (opt-in) | 0% | measurable | a keyed stage's prior_context bytes ≪ full prior_chunks (asserted in a test) |
| Dangling-input surfaced | never | yes | `config validate` reports a declared input not produced upstream |

## 5. User Stories

```
GIVEN context_routing_mode = full (default)
WHEN any existing pipeline runs
THEN every stage receives exactly today's prior_context (byte-identical) — no behaviour change

GIVEN context_routing_mode = keyed AND a role declares inputs: [design_spec, technical_spec]
WHEN its stage runs
THEN its prompt's prior context contains ONLY the stored design_spec and technical_spec outputs

GIVEN a producing role declares outputs: [design_spec] and its output contains a `## DESIGN_SPEC` section
WHEN the store is populated
THEN design_spec is keyed to that section's content (fine keying)

GIVEN a producing role's output has NO `## <KEY>` section matching an output name
WHEN the store is populated
THEN the whole stage blob is stored under each declared output key (coarse fallback)

GIVEN context_routing_mode = keyed AND a stage declares an input no upstream stage produces
WHEN config is validated
THEN validation fails closed with a clear dangling-input error

GIVEN context_routing_mode = keyed AND a declared input has no stored value at run time
WHEN the prompt is assembled
THEN routing falls back conservatively (include full prior_chunks) rather than sending empty context
```

## 6. Acceptance Criteria

- [ ] A `Settings.context_routing_mode: Literal["full","keyed"] = "full"` (config.py) gates everything. Default `full` = today's behaviour.
- [ ] A keyed store (`dict[str,str]`, output-key → content) is accumulated as stages run, ALONGSIDE `prior_chunks` (prior_chunks is retained for fallback).
- [ ] Store population: a stage's output is keyed by its role's `outputs`; if the output contains a `## <OUTPUT_KEY>` section (case-insensitive, reusing the `_parse_components` approach), that section is stored under the key; else the whole blob is stored under each declared output key (coarse fallback).
- [ ] In `keyed` mode, a stage whose role declares `inputs` gets a prior context assembled from ONLY the stored values for those input keys; a role with empty `inputs` still gets full `build_prior_context(prior_chunks)`.
- [ ] In `full` mode, prior context is computed exactly as today for ALL roles regardless of declared inputs (verified byte-identical).
- [ ] Conservative runtime fallback: in `keyed` mode, if a declared input key has no stored value, the stage falls back to full prior_chunks (never empty) and this is logged.
- [ ] `hivepilot config validate` reports a dangling input (a stage's role `input` not produced by any earlier stage's role `outputs` in that pipeline). Severity: WARNING by default; hard ERROR when `context_routing_mode=keyed`. Existing configs with cosmetic dangling inputs must still pass validation in `full` mode.
- [ ] `can_block` documented as advisory/superseded by stage-level `continue_on_failure` (A1); NOT wired here.
- [ ] Docs (`docs/v4/RUNBOOK.md`, `USAGE.md`) document `inputs`/`outputs` semantics, `context_routing_mode`, section headers, and the fallback rules.
- [ ] `python -m pytest -q` fully green; ruff + mypy clean.

## 7. Non-Goals (and why)

- **Changing the default behaviour.** A2 ships DORMANT (`full` mode). Flipping Noxys to `keyed` is a separate, later, opt-in change — not part of this PRD.
- **Rewriting existing prompts to emit `## <KEY>` sections.** Only the designer does today; all others keep working via coarse fallback. No prompt edits here.
- **Persisting the keyed store to the Obsidian vault / reading vault artifacts back.** Out of scope; the in-memory store suffices for intra-run routing.
- **Wiring `can_block`.** Redundant with `continue_on_failure` (A1). Document only.
- **Runner/model-layer changes** beyond the single `prior_context` injection point.
- **Removing `prior_chunks`.** Retained as the fallback substrate.

## 8. Technical Constraints

- Python, Pydantic v2, pytest `asyncio_mode="auto"`, ruff + mypy CI (Python 3.12).
- **BACKWARD-COMPAT IS THE TOP CONSTRAINT.** Every existing pipeline (company, noxys-v2, group) must behave byte-identically in the default `full` mode. Opt-in is the explicit mode flag ONLY — never input-presence.
- Reuse the `_parse_components` parsing style; hook at the single `prior_context` computation/injection point.

## 9. Architecture Decisions

| Decision | Reversal Cost | Alternatives | Rationale |
|----------|--------------|--------------|-----------|
| Opt-in via `context_routing_mode` flag (default `full`), NOT input-presence | High | trigger on declared inputs | Existing roles already declare inputs cosmetically → presence-trigger = mass regression (§2 danger) |
| Keep `prior_chunks`; add keyed store alongside | Med | replace prior_chunks | Retaining it is the safe fallback substrate; replacement risks regressions |
| Phased keying: coarse default + fine `## <KEY>` extraction with whole-blob fallback | Med | fine-only (needs all prompts to emit sections) | Coarse is backward-safe; fine routes the designer's sections precisely; fallback covers the rest |
| Conservative runtime fallback to full prior_chunks on missing key | Low | send empty | Never starve a stage of context (§2 danger) |
| Dangling-input: warn in `full`, error in `keyed` | Low | always error | Existing configs have cosmetic dangling inputs; hard error would break their `config validate` |
| Document `can_block` as advisory; don't wire | Low | wire can_block | Redundant with `continue_on_failure`; keeps A2 focused on routing |

## 10. Security Boundaries

- **Auth/tenant:** none new. Internal orchestration.
- **Trust boundaries:** pipeline/role YAML is operator-authored. The routing risk is a misrouted or
  dropped context; mitigated by conservative fallback + fail-closed validation under keyed mode.
- **Data sensitivity:** keyed routing can only ever route a SUBSET of what full mode already shows —
  it never widens exposure. No new data crosses any boundary.

## 11. Data Model

No datastore. In-memory + config-schema changes:
- `Settings.context_routing_mode: Literal["full","keyed"] = "full"` (config.py; env-overridable like other settings).
- Run-scoped keyed store: `dict[str, str]` (output-key → content), internal to the orchestrator run loop; not persisted.
- `Role.inputs`/`Role.outputs` unchanged in shape (`list[str]`) — now READ by the router.

**Access pattern:** as each stage completes, extract sections / store outputs by key. Before each
stage runs (keyed mode), gather `{k: store[k] for k in role.inputs if k in store}` → assemble
prior_context; if empty/missing → fall back to full prior_chunks.

## 12. Shared Contracts

- **`context_routing_mode`** setting name + values (`full`/`keyed`) — frozen.
- **Section header convention:** `## <OUTPUT_KEY>` (case-insensitive), matching the designer's
  `## DESIGN_SPEC` / `## UI_REVIEW`. Output keys map to headers by upper-casing / exact match — define the exact mapping in Sprint 1.
- **Fallback rules:** coarse whole-blob when no section; full prior_chunks when a key is missing at runtime.
- **Store keys** = producing role's `outputs` names.

## 13. Architecture Invariant Registry

| Concept | Owner | Format/Values | Verify Command |
| ------- | ----- | ------------- | -------------- |
| Default mode is `full` | config.py | `context_routing_mode` defaults `"full"` | `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && python -c "from hivepilot.config import Settings; assert Settings().context_routing_mode=='full'"` |
| Backward-compat (full mode unchanged) | orchestrator | existing pipeline tests green | `python -m pytest -q tests/test_pipeline_execution.py tests/test_company_pipeline.py tests/test_group_pipeline.py` |
| Router reads role.inputs only in keyed mode | orchestrator | routing gated by mode | new test `test_full_mode_ignores_inputs` exits 0 |
| Dangling input surfaced | config_validation | reported (warn/keyed-error) | new test `test_dangling_input_flagged` exits 0 |

**Dependency direction:** A2 owns `context_routing_mode` + routing semantics; consumes A1's roles/pipeline models unchanged.

## 14. Open Questions

- [ ] Exact output-key ↔ `## HEADER` mapping (upper-case the key? exact case-insensitive match on the key string?). Sprint 1 decides; must handle `design_spec` ↔ `## DESIGN_SPEC`.
- [ ] Whether `build_prior_context` modes (`cap`/`synthesis`) still apply to the keyed slice (recommend: apply the same `max_chars` cap to the assembled keyed context for safety). Sprint 2 decides.
- [ ] Should keyed mode be settable per-pipeline/per-role, not just global? Recommend global-only for v1 (simpler); note as a future extension.

## 15. Uncertainty Policy

- When uncertain whether a stage needs a piece of context: **include it** (conservative fallback).
- On a dangling input under keyed mode: **fail closed at validate time**.
- When backward-compat conflicts with cleaner routing: prefer **backward-compat** (default `full`).

## 16. Verification

- Deterministic: full `python -m pytest -q` green; ruff + mypy clean; the 4 INVARIANTS commands exit 0; a test asserting `full`-mode prior_context is byte-identical pre/post change; a test asserting a keyed stage's assembled context is strictly smaller and contains only declared inputs.
- Manual: reviewer confirms the router is unreachable in `full` mode (default) and that the fallback path logs when a key is missing.

## 17. Sprint Decomposition

### Sprint Overview

| Sprint | Title | Depends On | Batch | Model | Parallel With |
| ------ | ----- | ---------- | ----- | ------ | ------------- |
| 1 | Keyed store + section extraction | None | 1 | sonnet | — |
| 2 | Routing + `context_routing_mode` opt-in | Sprint 1 | 2 | sonnet | — |
| 3 | Dangling-input validation + docs | Sprint 1, 2 | 3 | sonnet | — |

Sprints 1 & 2 both touch `orchestrator.py` (shared) → sequential. Sprint 3 documents/validates the whole.

### Sprint 1: Keyed store + section extraction → `sprints/01-keyed-store.md`

**Objective:** Accumulate a run-scoped `dict[str,str]` (output-key → content) as stages run, using
`## <KEY>` section extraction with whole-blob coarse fallback. No routing/behaviour change yet (store
built but unused).
**Effort:** M · **Dependencies:** None

**File Boundaries:**
- `files_to_modify`: `hivepilot/orchestrator.py`, `tests/test_pipeline_execution.py`
- `files_read_only`: `hivepilot/roles.py`, `hivepilot/config.py`
- `shared_contracts`: store keys = role.outputs; `## <KEY>` header convention (§12)

**Tasks:**
- [ ] Add a `_parse_output_sections(text, keys)` helper (mirror `_parse_components:157`) returning `{key: section_text}` for any `## <KEY>` header present.
- [ ] In the stage loop, after a stage produces output, populate the keyed store: per-section where found, else whole blob under each `role.outputs` key.
- [ ] Store is built but NOT consumed yet (prior_chunks path unchanged) → zero behaviour change.
- [ ] Tests: section extraction, coarse fallback, multi-output keying, case-insensitive header match.

**Acceptance:** store populated correctly; existing behaviour unchanged (full suite green).
**Verification:** `python -m pytest -q tests/test_pipeline_execution.py` ; full `python -m pytest -q`.

### Sprint 2: Routing + opt-in mode → `sprints/02-routing.md`

**Objective:** Add `context_routing_mode` (default `full`); in `keyed` mode assemble a stage's prior
context from only its role's `inputs` (from the store), with conservative fallback; `full` mode
unchanged and byte-identical.
**Effort:** M · **Dependencies:** Sprint 1

**File Boundaries:**
- `files_to_modify`: `hivepilot/config.py`, `hivepilot/orchestrator.py`, `tests/test_pipeline_execution.py`, `tests/test_config.py`
- `files_read_only`: `hivepilot/runners/claude_runner.py` (hook point `:200-232`)
- `shared_contracts`: `context_routing_mode` values; fallback rules (§12)

**Tasks:**
- [ ] Add `Settings.context_routing_mode: Literal["full","keyed"] = "full"`.
- [ ] At the prior_context computation point (`orchestrator.py:1128`), branch: keyed + role.inputs → assemble from store by input keys (apply the same `max_chars` cap); else → today's `build_prior_context(prior_chunks)`.
- [ ] Conservative fallback: missing key → full prior_chunks; log it.
- [ ] Tests: `full` mode byte-identical (regression); keyed mode routes only declared inputs; keyed stage context strictly smaller; missing-key fallback.

**Acceptance:** §6 routing/fallback/mode criteria; full mode unchanged.
**Verification:** `python -m pytest -q tests/test_pipeline_execution.py tests/test_config.py` ; full suite green.

### Sprint 3: Dangling-input validation + docs → `sprints/03-validation-docs.md`

**Objective:** `config validate` flags dangling inputs (warn in full, error in keyed); document the
feature.
**Effort:** S · **Dependencies:** Sprint 1, 2

**File Boundaries:**
- `files_to_modify`: `hivepilot/services/config_validation.py`, `tests/test_config_validation.py`, `docs/v4/RUNBOOK.md`, `docs/v4/USAGE.md`
- `files_read_only`: `hivepilot/roles.py`, `hivepilot/config.py`
- `shared_contracts`: dangling-input severity rule (§9)

**Tasks:**
- [ ] In `validate_config`, for each pipeline walk stages in order, accumulate available output keys (from each stage's role.outputs), and flag any stage-role `input` not yet produced. Severity: warning by default, error under `context_routing_mode=keyed`.
- [ ] Ensure existing configs (cosmetic dangling inputs) still pass in `full` mode.
- [ ] Tests: dangling input flagged; clean config passes; keyed-mode escalates to error.
- [ ] Docs: `inputs`/`outputs` semantics, `context_routing_mode`, section headers, fallback rules, `can_block` advisory note.

**Acceptance:** §6 validation + docs criteria.
**Verification:** `python -m pytest -q tests/test_config_validation.py` ; `grep -n context_routing_mode docs/v4/*.md`.

## 18. Execution Log
[Filled during execution — tracked in progress.json]

## 19. Learnings (filled after all sprints complete)
[Compound step output]
