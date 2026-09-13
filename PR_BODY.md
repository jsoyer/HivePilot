## Summary

HP-95: Tool catalog with axes **risk × policy × volatile × idempotency** (Coworker + OpenSpace patterns only; ADR HP-94 accepted). First eng ticket after accept.

Owning issue: [HP-95](https://linear.app/js-workspace/issue/HP-95/u-01-tool-catalog-riskpolicyvolatileidempotency)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`.

Replay: `pytest tests/test_tool_catalog.py tests/test_hp58_typed_tools.py tests/test_config_files_roster.py tests/test_outward.py`

## What changed

1. **`hivepilot/tool_catalog.py` + `tool_catalog.yaml`** — intrinsic classification. Unknown token → deny (fail-closed). No catch-all `*`.
2. **YAML fields** are Linear/Coworker: `risk`, `defaultPolicy`, `volatile`, `idempotency`. `policies.yaml` `tool_policies` may override **policy** only — never `risk`.
3. **High/critical cannot widen to `allow`** without HP-86 `change_class=mechanical`. Tightening (deny) always allowed. HP-61 rules table is untouched.
4. **Orthogonal** to outward tokens (`hivepilot/outward.py`) and plugin capabilities (`network|filesystem|subprocess|secrets_access|env`). Not merged into risk tiers.
5. **`GET /v1/tools` unchanged** — HP-58 typed-tool listing; catalog resolution is a separate helper (`resolve` / `resolve_typed_tool`) for the HP-94 Approvals inbox `kind=tool`.

## Testing

- [x] `pytest tests/test_tool_catalog.py` — 23 passed (unknown deny; override ≠ risk; high→allow gated; orthogonality; YAML load; known resolve)
- [x] `pytest tests/test_hp58_typed_tools.py` — 15 passed (`GET /v1/tools` still works)
- [x] `ruff check` + `ruff format --check` clean
