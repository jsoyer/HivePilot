## Summary

HP-96 (Linear acceptance): exact workspace text edits with a required revision lock, plus isolated JSON memory (data, not instructions). Coworker + OpenSpace patterns only; ADR HP-94 accepted.

Owning issue: [HP-96](https://linear.app/js-workspace/issue/HP-96/u-02-workspace-text-revision-isolation-json-memoire)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`.

Replay: `pytest tests/test_workspace_text.py tests/test_tool_catalog.py tests/test_skill_catalog.py`

## What changed

1. **`hivepilot/workspace_text.py`** — Coworker `workspace-context.ts` / `applyWorkspaceTextEdit` **patterns**, rewritten. Empty `oldText` appends; non-unique match refuses; `expectedRevision` required; overflow bound.
2. **Isolated JSON memory** — stored/recalled as `role=data`. No path renders memory as instructions or merges it into `extra_prompt`.
3. **Tests** — append / replace / remove / duplicate / stale / overflow + isolation.
4. **Docs** — pointers in `docs/ARCHITECTURE.md` and `docs/SECURITY.md`.

`tool_catalog` and `skill_catalog` are untouched.

## Out of scope

- HP-97 PASS store
- HP-99 evidence refs
- HP-101 memory HITL proposals
- HP-105 trust provisional↔trusted
- WhatsApp
- HP-67 desktop-per-agent

## Testing

- [x] append / replace / remove / duplicate / stale / overflow
- [x] isolation: memory = JSON data, not instructions
- [x] `pytest tests/test_workspace_text.py`
- [x] `pytest tests/test_tool_catalog.py tests/test_skill_catalog.py` (catalogs still green)
- [x] `ruff check` + `ruff format --check` on the new files
