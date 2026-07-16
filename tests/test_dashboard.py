"""Tests for hivepilot.ui.dashboard — skipped when textual is not installed."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual.app")

from hivepilot.ui.dashboard import RunDashboard  # noqa: E402


def test_refresh_interactions_method_exists() -> None:
    assert hasattr(RunDashboard, "refresh_interactions")


def test_refresh_interactions_is_callable() -> None:
    assert callable(getattr(RunDashboard, "refresh_interactions"))


@pytest.mark.asyncio
async def test_mount_with_a_run_present_does_not_raise_on_row_highlight() -> None:
    """Regression: on_mount -> refresh_runs() highlights row 0 whenever any
    run exists, firing a DataTable.RowHighlighted event. The handler used to
    read `event.table.id` — textual's actual attribute is `event.data_table`
    — so this crashed with AttributeError on every real dashboard use as
    soon as a run existed. `_isolate_state_db` (conftest, autouse) already
    redirects state_service.DB_PATH to a per-test tmp file."""
    from hivepilot.services import state_service

    state_service.record_run_start("acme", "sometask")

    app = RunDashboard()
    async with app.run_test():
        assert app.runs_table.row_count == 1


@pytest.mark.asyncio
async def test_refresh_metrics_reconciles_success_and_complete_as_success() -> None:
    """Phase 24a: dashboard's success/failure counters must use the same
    canonical outcome mapping as analytics_service (RunStatus.COMPLETE ==
    'complete' must count as a success, not a failure)."""
    from hivepilot.services import state_service

    state_service.record_run_start("acme", "task1", status="success")
    run2 = state_service.record_run_start("acme", "task2", status="running")
    state_service.complete_run(run2, "complete")

    app = RunDashboard()
    async with app.run_test():
        rows = {
            app.metrics_table.get_cell_at((r, 0)): app.metrics_table.get_cell_at((r, 1))
            for r in range(app.metrics_table.row_count)
        }
        assert rows["total_runs"] == "2"
        assert rows["success"] == "2"
        assert rows["failure"] == "0"


@pytest.mark.asyncio
async def test_refresh_metrics_counts_true_failures() -> None:
    from hivepilot.services import state_service

    run1 = state_service.record_run_start("acme", "task1", status="running")
    state_service.complete_run(run1, "failed")

    app = RunDashboard()
    async with app.run_test():
        rows = {
            app.metrics_table.get_cell_at((r, 0)): app.metrics_table.get_cell_at((r, 1))
            for r in range(app.metrics_table.row_count)
        }
        assert rows["failure"] == "1"
        assert rows["success"] == "0"
