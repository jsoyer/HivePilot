## Summary

HP-122 — observability join from a pipeline/run verdict to the human HITL decision that consumed it (approve / reject / edit). This is not the PASS store (HP-97/101). `agreement_rows` still joins on `pipeline_run_id`, which stays empty when a review and a human gate never share a run. The new table records the link explicitly.

Owning issue: [HP-122](https://linear.app/js-workspace/issue/HP-122/r5-mesure-lien-verdict-decision-humaine)

Replay: `pytest tests/test_verdict_hitl_link.py -q`

## How the join is stored and queried

Table `verdict_hitl_links` (created in `state_service.init_db`):

- Key: `run_id` + `step` + `approval_id`
- `decision`: `approve` | `reject` | `edit`
- `verdict_id`: the consumed verdict, or NULL when the run had none

`verdict_hitl.trace_hitl_decision(run_id, step, approval_id)` returns that row plus the verdict. `record_verdict_hitl_link` writes it. The consumed verdict is the latest row on that run (`pipeline_run_id` preferred over `run_id`). A verdict on a different run is not guessed.

`Orchestrator.approve_run` snapshots that verdict before dispatch, then writes the link after the human decision is stored. A verdict recorded while the pipeline resumes is not the one the human consumed. HP-61 auto-deny (`approved_by=rule`) is not a human decision and is not linked.

Queries:

- `decisions_without_verdict()` — link rows with a NULL or dangling `verdict_id`, plus terminal human approvals that were never linked
- `verdicts_without_decision()` — verdicts with a non-null `decision` that no link consumed

## Doctor finding names

`hivepilot config doctor` (live state DB only) runs `check_verdict_hitl_join`:

- `décisions sans verdict` — warning
- `verdicts sans décision` — warning

Both are silent when the tables are empty or every decision points at a live verdict and every consumable verdict is consumed. A NULL `verdicts.decision` (fail-closed review with no parsed outcome) is not a consumable verdict.

## Out of scope

- Rebuilding the PASS / memory store
- HP-124 OTLP smoke, HP-131 tenant vault follow-up
- Telegram cutover

## Testing

- [x] `pytest tests/test_verdict_hitl_link.py -q` — 6 passed
- [x] `pytest tests/test_autonomy_service.py tests/test_state_service.py::TestVerdictsCanBeJoinedToApprovals tests/test_cli_approvals.py tests/test_partition_service.py::TestApproveRunPartitionRoute tests/test_config_doctor.py::TestRetryQueueBacklog::test_wired_into_run_doctor tests/test_doctor_liveness.py -q` — passed
- [x] `ruff check` + `ruff format --check` on the touched Python files
