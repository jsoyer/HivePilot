## Summary

HP-56: DB-backed **routines** per role. Skills stay files; YAML `schedules.yaml` stays the interval daemon. This slice is backend only (HP-57 is the editor).

- Table `routines`: `crons[]`, IANA `timezone`, persisted `next_run_at` / `last_run_at`, optional `replace_key`.
- Dedup: `UNIQUE (tenant, replace_key)` — a second POST with the same key updates the existing row (id stable).
- Execution: scheduler daemon `_run_due_routines()` → role `command_task` via `Orchestrator.run_task`, same retry-queue contract as `run_entry` (`routine:{id}`). Cadence is always stamped so a failing routine cannot busy-loop.
- API: `GET/POST/PATCH/DELETE /v1/routines`, `POST /v1/webhook/routines/{id}` (id or replace_key). Read = `read`, write/fire = `run`.

Linear: [HP-56](https://linear.app/js-workspace/issue/HP-56/modele-routines-par-role-cronstimezonewebhook-dedup-replacekey).

Replay: `POST /v1/routines` with `role=developer`, `crons=["0 9 * * 1"]`, `timezone=Europe/Paris`, `replace_key=weekly-dev`, then `hivepilot schedule daemon` (or `POST /v1/webhook/routines/weekly-dev`).

## Testing

- [x] `pytest tests/test_routine_service.py tests/test_routines_api.py tests/test_scheduler_daemon.py::TestSchedulerDaemonRoutines` (16 passed)
- [x] `ruff check` / `ruff format` on touched files
- [x] `python -m mypy hivepilot/services/routine_service.py hivepilot/services/scheduler_daemon.py hivepilot/services/api_service.py`
- [x] `python scripts/export_openapi.py --check` after regenerating `web/openapi.json`
