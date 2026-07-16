"""Tests for hivepilot.ui.dashboard — skipped when textual is not installed."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual.app")

from textual.coordinate import Coordinate  # noqa: E402

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
            app.metrics_table.get_cell_at(Coordinate(r, 0)): app.metrics_table.get_cell_at(
                Coordinate(r, 1)
            )
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
            app.metrics_table.get_cell_at(Coordinate(r, 0)): app.metrics_table.get_cell_at(
                Coordinate(r, 1)
            )
            for r in range(app.metrics_table.row_count)
        }
        assert rows["failure"] == "1"
        assert rows["success"] == "0"


def _cost_rows_by_scope(app: RunDashboard) -> dict[str, list[str]]:
    return {
        app.cost_table.get_cell_at(Coordinate(r, 0)): [
            app.cost_table.get_cell_at(Coordinate(r, c)) for c in range(6)
        ]
        for r in range(app.cost_table.row_count)
    }


@pytest.mark.asyncio
async def test_refresh_cost_method_exists_and_is_callable() -> None:
    assert hasattr(RunDashboard, "refresh_cost")
    assert callable(getattr(RunDashboard, "refresh_cost"))


@pytest.mark.asyncio
async def test_refresh_cost_populates_overall_and_provider_breakdown() -> None:
    """Seeds one priced step (price-map-covered model, no self-reported cost_usd
    -> falls back to pricing.estimate_cost) and one unpriced step (unknown
    model), then asserts the Cost table's overall row aggregates totals/cost
    correctly and reports the unpriced-step coverage, and that a per-provider
    breakdown row exists."""
    from hivepilot.services import state_service

    run_id = state_service.record_run_start("acme", "task1")
    state_service.record_step(
        run_id,
        "generate",
        "success",
        provider="claude",
        model="claude-sonnet-4-6",
        input_tokens=100_000,
        output_tokens=50_000,
    )
    state_service.record_step(
        run_id,
        "review",
        "success",
        provider="claude",
        model="unpriced-model",
        input_tokens=10,
        output_tokens=10,
    )

    app = RunDashboard()
    async with app.run_test():
        by_scope = _cost_rows_by_scope(app)

        overall = by_scope["overall"]
        assert overall[1] == "2"  # total_steps
        assert overall[2] == "100010"  # input_tokens
        assert overall[3] == "50010"  # output_tokens
        # (100_000/1e6)*3.0 + (50_000/1e6)*15.0 == 1.05, unpriced step contributes 0.0
        assert overall[4] == "1.05"  # cost_usd
        assert overall[5] == "1"  # unpriced_steps

        provider_row = by_scope["provider:claude"]
        assert provider_row[1] == "2"
        assert provider_row[4] == "1.05"

        model_row = by_scope["model:claude-sonnet-4-6"]
        assert model_row[1] == "1"
        assert model_row[5] == "0"


@pytest.mark.asyncio
async def test_refresh_cost_with_no_steps_shows_zeroed_overall_row() -> None:
    app = RunDashboard()
    async with app.run_test():
        by_scope = _cost_rows_by_scope(app)
        assert app.cost_table.row_count == 1
        overall = by_scope["overall"]
        assert overall[1] == "0"
        assert overall[2] == "0"
        assert overall[3] == "0"
        assert overall[4] == "0.0"
        assert overall[5] == "0"
