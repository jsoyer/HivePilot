# INVARIANTS — Keyed Context Routing (PRD A2)

Machine-verifiable contracts. Run each `Verify` from the repo root
`/home/jeromesoyer/Documents/Github/jsoyer/HivePilot`. All must exit 0 before A2 is complete.

## Default mode is `full` (ships dormant)
- **Owner:** `hivepilot/config.py`
- **Postconditions:** `Settings().context_routing_mode == "full"`.
- **Invariants:** the routing feature is unreachable unless explicitly enabled.
- **Verify:** `python -c "from hivepilot.config import Settings; assert Settings().context_routing_mode=='full'"`
- **Fix:** default the field to `"full"`.

## Backward-compat: full mode unchanged for all pipelines
- **Owner:** `hivepilot/orchestrator.py`
- **Preconditions:** `context_routing_mode=full` (default); roles may declare inputs (they do, cosmetically).
- **Postconditions:** every stage's prior_context is computed exactly as before — routing is NOT triggered by input-presence.
- **Verify:** `python -m pytest -q tests/test_pipeline_execution.py tests/test_company_pipeline.py tests/test_group_pipeline.py`
- **Fix:** gate routing solely on `context_routing_mode == "keyed"`; never on `role.inputs` presence.

## Store keying: sections when present, whole-blob fallback otherwise
- **Owner:** `hivepilot/orchestrator.py` (`_parse_output_sections` + store population)
- **Postconditions:** an output with a `## <KEY>` section stores that section under the key; without a section, the whole blob is stored under each declared output key.
- **Verify:** `python -m pytest -q -k "output_sections or coarse_fallback or keyed_store"`
- **Fix:** implement `_parse_output_sections` mirroring `_parse_components`; apply coarse fallback per missing key.

## Conservative runtime fallback (never empty context)
- **Owner:** `hivepilot/orchestrator.py`
- **Preconditions:** keyed mode; a stage's declared input keys absent from the store.
- **Postconditions:** the stage receives full prior_chunks (logged), NOT empty context.
- **Verify:** `python -m pytest -q -k "missing_key_fallback or keyed_fallback"`
- **Fix:** when the keyed slice would be empty, fall back to `build_prior_context(prior_chunks, ...)`.

## Dangling input surfaced; does not break full-mode validate
- **Owner:** `hivepilot/services/config_validation.py`
- **Postconditions:** a declared input not produced upstream is reported (warning in full, error in keyed); existing configs with cosmetic dangling inputs still pass `config validate` in full mode.
- **Verify:** `python -m pytest -q -k "dangling_input"`
- **Fix:** walk stages accumulating role.outputs; check role.inputs; severity by mode.
