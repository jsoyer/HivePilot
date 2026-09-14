## Summary

HP-118: local task-trace export. A project run projects to metadata / tools / skills / redaction and writes a ZIP on demand. Critical findings block the export (no archive). There is no cloud reporter or upload path.

Owning issue: [HP-118](https://linear.app/js-workspace/issue/HP-118/u-24-task-traces-export-local-no-upload)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-99 evidence/redaction and HP-104 skill-cycle events. Patterns only from OpenSpace local traces.

Replay: `hivepilot traces export <run_id> --output traces.zip`

## What changed

1. **`hivepilot/trace_export.py`** — `project_run` / `export_zip`. Local ZIP only; remote URLs refused.
2. **`hivepilot traces export`** — CLI on-demand write. Exit 1 on critical findings or missing run.
3. **Docs** — SECURITY / SKILLS / ARCHITECTURE / CLI-REFERENCE record the local-only gate.

## Out of scope

- Cloud reporters, upload APIs, OpenSpace sync
- WhatsApp, HP-67, autonomous evolve
- Dashboard download button

## Testing

- [x] `pytest tests/test_trace_export.py` — redaction, critical-block, local-only, CLI
- [x] `ruff check` + `ruff format --check` clean on touched Python
