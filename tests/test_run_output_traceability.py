"""Regression tests for finding a successful run's output after the fact.

Second half of the run-267 incident (`noxys-ciso`): the report never reached
the operator, and once delivery failed there was no way back to it. The run row
said "success" and held nothing else — a successful run records no ``detail``,
and the artifact directory it wrote was referenced nowhere in the database. On
top of that, the concierge's grounding snapshot showed only ``started_at``, so
the classifier could not tell a finished run from a running one and guessed
wrong out loud ("just dispatched, no time to produce results yet" — four
minutes after the run had succeeded).
"""

from __future__ import annotations

from hivepilot.services import concierge_service, state_service

# ---------------------------------------------------------------------------
# attach_run_artifacts — a successful run must point at its own output
# ---------------------------------------------------------------------------


def test_successful_run_points_at_the_artifacts_that_hold_its_output():
    run_id = state_service.record_run_start("noxys", "noxys-ciso")
    state_service.complete_run(run_id, "success")

    assert not (state_service.get_run(run_id) or {}).get("detail"), (
        "precondition: a successful run records no detail — that is the gap being closed"
    )

    state_service.attach_run_artifacts(run_id, "/runs/20260801-064733")

    detail = (state_service.get_run(run_id) or {}).get("detail") or ""
    assert "/runs/20260801-064733" in detail
    assert (state_service.get_run(run_id) or {}).get("status") == "success", (
        "attaching a pointer must not disturb the run's status"
    )


def test_a_failure_message_is_never_overwritten_by_the_artifact_pointer():
    """The failure reason is the more valuable record — it must win."""
    run_id = state_service.record_run_start("noxys", "groomer-scan")
    state_service.complete_run(run_id, "failed", "claude exited 143")

    state_service.attach_run_artifacts(run_id, "/runs/20260801-090000")

    detail = (state_service.get_run(run_id) or {}).get("detail") or ""
    assert "claude exited 143" in detail
    assert "/runs/20260801-090000" not in detail


# ---------------------------------------------------------------------------
# _grounding_snapshot — a finished run must not read as an in-flight one
# ---------------------------------------------------------------------------


def test_snapshot_marks_a_finished_run_as_finished():
    run_id = state_service.record_run_start("noxys", "noxys-ciso")
    state_service.complete_run(run_id, "success")

    snapshot = concierge_service._grounding_snapshot()

    assert "noxys-ciso" in snapshot
    assert "finished" in snapshot, (
        "without a finished time the classifier cannot distinguish a completed "
        "run from a running one, and it invents an answer"
    )
    assert "STILL RUNNING" not in snapshot


def test_snapshot_marks_an_unfinished_run_as_still_running():
    state_service.record_run_start("noxys", "noxys-qa")  # never completed

    snapshot = concierge_service._grounding_snapshot()

    assert "noxys-qa" in snapshot
    assert "STILL RUNNING" in snapshot


def test_snapshot_never_raises_and_says_so_when_there_is_nothing():
    """Grounding is best-effort — it must degrade, never break the concierge."""
    snapshot = concierge_service._grounding_snapshot()
    assert isinstance(snapshot, str)
    assert snapshot  # never empty; "(no recent runs or pending approvals)" when bare
