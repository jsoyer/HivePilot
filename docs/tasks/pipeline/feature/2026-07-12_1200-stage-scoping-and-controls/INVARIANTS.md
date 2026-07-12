# INVARIANTS — Stage Scoping & Controls (PRD A1)

Machine-verifiable contracts for this PRD. Run each `Verify` from the repo root
`/home/jeromesoyer/Documents/Github/jsoyer/HivePilot`. All must exit 0 before A1 is complete.

## Backward Compatibility (no opt-in → unchanged behaviour)
- **Owner:** `hivepilot/orchestrator.py` (stage loop)
- **Preconditions:** a stage declares none of `only_components`, `only_tags`, `continue_on_failure`.
- **Postconditions:** it runs exactly as before; failure still fail-fast-breaks.
- **Invariants:** existing pipeline/company/component tests are unmodified in intent and pass.
- **Verify:** `python -m pytest -q tests/test_pipeline_execution.py tests/test_company_pipeline.py tests/test_component_selection.py`
- **Fix:** ensure new fields are optional with safe defaults and the skip branch is entered only when a selector is set.

## New PipelineStage fields optional with safe defaults
- **Owner:** `hivepilot/models.py` (`PipelineStage`)
- **Postconditions:** `only_components is None`, `only_tags is None`, `continue_on_failure is False` by default.
- **Verify:** `python -c "from hivepilot.models import PipelineStage as S; s=S(name='x',task='t'); assert s.only_components is None and s.only_tags is None and s.continue_on_failure is False"`
- **Fix:** add fields with `= None` / `= False` defaults.

## Group.tags present and typed
- **Owner:** `hivepilot/models.py` (`Group`)
- **Postconditions:** `Group.tags` defaults to `{}`, typed `dict[str, list[str]]`.
- **Verify:** `python -c "from hivepilot.models import Group as G; g=G(description='d',hub='h',components=[]); assert g.tags=={}"`
- **Fix:** add `tags: dict[str, list[str]] = {}` (use `Field(default_factory=dict)` if Pydantic requires it).

## Undefined tag fails closed
- **Owner:** config/pipeline resolution
- **Preconditions:** a stage sets `only_tags: [X]` where `X` is not a key in the run's `Group.tags`.
- **Postconditions:** a clear error is raised (naming `X`) at load/resolve — NOT a silent skip.
- **Invariants:** a security/review stage is never silently bypassed by misconfiguration.
- **Verify:** `python -m pytest -q -k undefined_tag` (test `test_undefined_tag_raises` must exist and pass)
- **Fix:** validate `only_tags` against `Group.tags` before the skip computation and `raise ValueError`.

## continue_on_failure activates the latent hook
- **Owner:** `hivepilot/orchestrator.py:1183-1192`
- **Postconditions:** `continue_on_failure=true` suppresses the fail-fast `break`; `false`/absent preserves it.
- **Verify:** `python -m pytest -q -k continue_on_failure` (tests for both true and false must exist and pass)
- **Fix:** reference `stage.continue_on_failure` now that the field exists; keep the `and not ...` guard.
