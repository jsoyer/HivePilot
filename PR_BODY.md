## Summary

HP-116: centralize workspace path confinement (relative only, block `..`, realpath/symlink must stay inside the root) and route `schedules.create` through the HP-97 PASS inbox with class ≠ mechanical.

Owning issue: [HP-116](https://linear.app/js-workspace/issue/HP-116/u-22-pathsymlink-confine-schedulescreate-hitl)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-95 catalog, HP-97 PASS, HP-110 path checks. Patterns only from Coworker workspace-path + `schedules.create` HITL.

Replay: `pytest tests/test_workspace_paths.py tests/test_schedule_create.py tests/test_skill_evolution_validator.py tests/test_pass_store.py tests/test_tool_catalog.py`

## What changed

1. **`hivepilot/workspace_paths.py`** — `confine` / `normalize_relpath`. Lexical relative + no `..`; `Path.resolve` and `os.path.realpath` must remain inside the workspace root.
2. **`hivepilot/schedule_create.py`** — `request` / `approve`. Token `schedules.create`, stored class always `product_fork`. YAML write only after APPROVED.
3. **`tool_catalog.yaml`** — `schedules.create` (high / require_approval).
4. **HP-110** — skill-evolution path checks reuse the shared lexical + realpath helper.
5. **Docs** — SECURITY / ARCHITECTURE.

## Out of scope

- HP-67 sandbox, WhatsApp, cloud OpenSpace, autonomous evolve
- Rewriting the scheduler daemon or Telegram doors

## Testing

- [ ] `pytest tests/test_workspace_paths.py tests/test_schedule_create.py tests/test_skill_evolution_validator.py tests/test_pass_store.py tests/test_tool_catalog.py`
