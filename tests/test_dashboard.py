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
