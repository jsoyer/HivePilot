## Summary

HP-57: Pollen **Automations** widget on Autopilot — one surface for role routines (HP-56) and YAML `schedules.yaml`, sharing the scheduler daemon tick.

- Editor: create a routine (role / crons / timezone / projects / replace_key), run now, enable/disable, delete.
- Existing named-schedules card stays (index 05); routines sit above it.
- `DELETE` added to API CORS so the editor can retire a routine from the Vite/dev origin.

Linear: [HP-57](https://linear.app/js-workspace/issue/HP-57/editeur-de-routines-ui-unification-schedulerautopilot-widget).

Replay: open Pollen → Operate → Autopilot, save a routine with `role=developer`, `crons=0 9 * * 1`, `replace_key=weekly-dev`.

## Testing

- [x] `cd web && npm test -- --run src/components/views/RoutinesCard.test.tsx src/components/views/SchedulesCard.test.tsx src/components/views/AutopilotView.test.tsx src/components/Pollen.test.tsx` (56 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-DOggT_RQ.js`)
- [x] `ruff check` on `api_service.py` CORS change
