## Summary

HP-97: unified PASS store for tool / memory / skill_evolution (and partition) proposals. One inbox; `kind` is the HP-94 discriminant, partitioned from the HP-61 action token. Decision persisted **before** side-effect. Edit cannot retarget path/revision. `match_auto` composes HP-95 tool-catalog policy + HP-61 rules + HP-86 mechanical gate.

Owning issue: [HP-97](https://linear.app/js-workspace/issue/HP-97/u-03-pass-store-unifie-tool-memory-skill-proposals)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-61 / HP-86 / HP-95 / HP-96.

Replay: `pytest tests/test_pass_store.py tests/test_hp61_approval_rules.py tests/test_tool_catalog.py`

## What changed

1. **`hivepilot/pass_store.py`** — create pending → `decide(approve/reject/edit/expire)` → inbox filtered by `kind`. Statuses `PENDING|APPROVED|REJECTED|EDITED|EXPIRED`.
2. **Persist before side-effect** — `decide` commits, then runs an optional callback. A raising callback cannot roll back the status.
3. **Edit freeze** — `path` / `revision` / `expected_revision` cannot be retargeted.
4. **`match_auto` compose** — catalog deny wins; catalog `require_approval` stays HITL; only HP-86 `mechanical` may auto-approve. HP-94 `kind` is stored as `pass_kind` so it does not collide with HP-61's action `kind`.
5. **SQLite `pass_proposals`** — the run-keyed `approvals` table is not reused (`PRIMARY KEY(run_id)`).

## Out of scope

- HP-99 evidence, HP-100 idempotency/checkpoints, HP-101 memory HITL apply, HP-105 trust
- WhatsApp, HP-67 sandbox, presenter/Telegram door wiring (HP-102)

## Testing

- [x] `pytest tests/test_pass_store.py tests/test_hp61_approval_rules.py tests/test_tool_catalog.py tests/test_workspace_text.py` — 88 passed
- [x] `ruff check` + `ruff format --check` clean
