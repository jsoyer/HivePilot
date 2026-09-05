"""Tests for hivepilot.ui.dashboard — skipped when textual is not installed."""

from __future__ import annotations

from typing import Any

import pytest

textual = pytest.importorskip("textual.app")

from textual.coordinate import Coordinate  # noqa: E402

from hivepilot.plugins import HealthStatus  # noqa: E402
from hivepilot.ui.dashboard import RunDashboard  # noqa: E402


def _cell_plain(value: Any) -> str:
    """Normalize a DataTable cell to plain text — cells may be a raw `str`
    (most tables) or a `rich.text.Text` (the Health tab's colored status
    badge, see `dashboard._health_status_cell`)."""
    return value.plain if hasattr(value, "plain") else str(value)


def test_refresh_interactions_method_exists() -> None:
    assert hasattr(RunDashboard, "refresh_interactions")


def test_refresh_interactions_is_callable() -> None:
    assert callable(getattr(RunDashboard, "refresh_interactions"))


def test_title_is_pollen() -> None:
    assert RunDashboard.TITLE == "Pollen"


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


# ---------------------------------------------------------------------------
# Pollen tabbed layout — Sprint: Analytics / Cost / Health tabs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_has_all_tabbed_tables() -> None:
    """Every tab's table exists after mount, and the pre-existing tables
    still populate (regression: the tabbed layout must not drop any of the
    folded-in Analytics tables)."""
    from hivepilot.services import state_service

    state_service.record_run_start("acme", "sometask")

    from textual.widgets import DataTable

    app = RunDashboard()
    async with app.run_test():
        assert app.metrics_table.row_count > 0
        assert app.runs_table.row_count == 1
        assert isinstance(app.hotspots_table, DataTable)
        assert isinstance(app.steps_table, DataTable)
        assert isinstance(app.interactions_table, DataTable)
        assert app.cost_table.row_count == 1
        assert isinstance(app.health_table, DataTable)


@pytest.mark.asyncio
async def test_refresh_hotspots_populates_from_step_failures() -> None:
    from hivepilot.services import state_service

    run_id = state_service.record_run_start("acme", "task1")
    state_service.record_step(run_id, "build", "failed")
    state_service.record_step(run_id, "build", "failed")
    state_service.record_step(run_id, "deploy", "success")

    app = RunDashboard()
    async with app.run_test():
        rows = {
            (
                _cell_plain(app.hotspots_table.get_cell_at(Coordinate(r, 0))),
                _cell_plain(app.hotspots_table.get_cell_at(Coordinate(r, 1))),
            ): _cell_plain(app.hotspots_table.get_cell_at(Coordinate(r, 2)))
            for r in range(app.hotspots_table.row_count)
        }
        assert rows[("build", "failed")] == "2"
        # Highest-failure-count combinations sort first (analytics_service
        # convention) — the failing "build" row must be row 0.
        assert _cell_plain(app.hotspots_table.get_cell_at(Coordinate(0, 0))) == "build"


# ---------------------------------------------------------------------------
# Runs / Steps tables — fix/linear-sync-display-time sweep: `refresh_runs`
# (Started/Finished columns) and `refresh_steps` (Timestamp column) were
# found echoing the raw stored value verbatim, same bug class as the
# pre-fix `schedule health`/`linear sync` CLI tables.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_display_timezone_override(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "display_timezone", None, raising=False)


@pytest.mark.asyncio
async def test_refresh_runs_renders_local_time_with_marker_not_raw_utc(monkeypatch) -> None:
    """The exact bug: a stored 09:08 UTC run must render as 11:08 CEST in
    the Started column -- a bare, unmarked 09:08 is precisely how the
    original bug hid from an operator reading their local clock."""
    from hivepilot.config import settings
    from hivepilot.services import state_service

    monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
    fake_run = {
        "id": 1,
        "project": "acme-web",
        "task": "deploy",
        "status": "success",
        "started_at": "2026-07-27 09:08:32",
        "finished_at": "2026-07-27 09:10:00",
    }
    monkeypatch.setattr(state_service, "list_recent_runs", lambda *a, **k: [fake_run])
    monkeypatch.setattr(state_service, "get_steps_for_run", lambda run_id: [])

    app = RunDashboard()
    async with app.run_test():
        started = _cell_plain(app.runs_table.get_cell_at(Coordinate(0, 4)))
        finished = _cell_plain(app.runs_table.get_cell_at(Coordinate(0, 5)))

    assert "09:08" not in started
    assert "11:08" in started
    assert "CEST" in started
    assert "09:10" not in finished
    assert "11:10" in finished


@pytest.mark.asyncio
async def test_refresh_runs_dst_correctness_summer_vs_winter(monkeypatch) -> None:
    """Same wall-clock UTC time renders differently in July (CEST) vs
    January (CET) -- a fixed-offset shortcut fails this."""
    from hivepilot.config import settings
    from hivepilot.services import state_service

    monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
    monkeypatch.setattr(state_service, "get_steps_for_run", lambda run_id: [])

    async def _started_for(started_at: str) -> str:
        fake_run = {
            "id": 1,
            "project": "acme-web",
            "task": "deploy",
            "status": "success",
            "started_at": started_at,
            "finished_at": None,
        }
        monkeypatch.setattr(state_service, "list_recent_runs", lambda *a, **k: [fake_run])
        app = RunDashboard()
        async with app.run_test():
            return _cell_plain(app.runs_table.get_cell_at(Coordinate(0, 4)))

    summer = await _started_for("2026-07-27 09:08:32")
    winter = await _started_for("2026-01-27 09:08:32")

    assert "11:08" in summer and "CEST" in summer
    assert "10:08" in winter and "CET" in winter
    assert summer != winter


@pytest.mark.asyncio
async def test_refresh_runs_missing_finished_at_renders_empty_not_fabricated(
    monkeypatch,
) -> None:
    """A still-running row (no `finished_at`) must render an empty cell,
    never a fabricated time -- distinct from `to_display`'s `(unknown)`
    marker for a genuinely-unparseable value, matching the pre-existing
    `run.get("finished_at") or ""` contract."""
    from hivepilot.config import settings
    from hivepilot.services import state_service

    monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
    fake_run = {
        "id": 1,
        "project": "acme-web",
        "task": "deploy",
        "status": "running",
        "started_at": "2026-07-27 09:08:32",
        "finished_at": None,
    }
    monkeypatch.setattr(state_service, "list_recent_runs", lambda *a, **k: [fake_run])
    monkeypatch.setattr(state_service, "get_steps_for_run", lambda run_id: [])

    app = RunDashboard()
    async with app.run_test():
        finished = _cell_plain(app.runs_table.get_cell_at(Coordinate(0, 5)))

    assert finished == ""


@pytest.mark.asyncio
async def test_refresh_steps_timestamp_renders_local_time_with_marker(monkeypatch) -> None:
    from hivepilot.config import settings
    from hivepilot.services import state_service

    monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
    fake_run = {
        "id": 1,
        "project": "acme-web",
        "task": "deploy",
        "status": "success",
        "started_at": "2026-07-27 09:08:32",
        "finished_at": "2026-07-27 09:10:00",
    }
    fake_step = {
        "run_id": 1,
        "step": "build",
        "status": "success",
        "detail": "",
        "timestamp": "2026-07-27 09:09:00",
    }
    monkeypatch.setattr(state_service, "list_recent_runs", lambda *a, **k: [fake_run])
    monkeypatch.setattr(state_service, "get_steps_for_run", lambda run_id: [fake_step])

    app = RunDashboard()
    async with app.run_test():
        ts = _cell_plain(app.steps_table.get_cell_at(Coordinate(0, 4)))

    assert "09:09" not in ts
    assert "11:09" in ts
    assert "CEST" in ts


# ---------------------------------------------------------------------------
# Health tab
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_health_renders_injected_statuses_without_crash() -> None:
    """Injected fake health set (constructor override, same shape as
    `PluginManagerApp`) — an "error" entry (as `PluginManager.
    run_health_check` would produce for a raising check) must render, not
    crash the dashboard."""
    fake_health = {
        "good_plugin": HealthStatus("ok", "all good"),
        "broken_plugin": HealthStatus("error", "RuntimeError: boom"),
        "dormant_plugin": HealthStatus("degraded", "installed but disabled"),
    }

    app = RunDashboard(health=fake_health)
    async with app.run_test():
        rows = {
            _cell_plain(app.health_table.get_cell_at(Coordinate(r, 0))): (
                _cell_plain(app.health_table.get_cell_at(Coordinate(r, 1))),
                _cell_plain(app.health_table.get_cell_at(Coordinate(r, 2))),
            )
            for r in range(app.health_table.row_count)
        }
        assert rows["good_plugin"] == ("ok", "all good")
        assert rows["broken_plugin"] == ("error", "RuntimeError: boom")
        assert rows["dormant_plugin"] == ("degraded", "installed but disabled")


@pytest.mark.asyncio
async def test_refresh_health_with_real_plugin_manager_does_not_crash() -> None:
    """No injected override -> reads from a real `Orchestrator().plugins.
    check_all()` (the shipped example plugins' health checks — headroom/
    rtk/obsidian — are all fast/local, gated behind `*_enabled`
    settings that default False, so this never makes a network call in
    CI). Every rendered status must be one of the three valid values."""
    app = RunDashboard()
    async with app.run_test():
        for r in range(app.health_table.row_count):
            status = _cell_plain(app.health_table.get_cell_at(Coordinate(r, 1)))
            assert status in ("ok", "degraded", "error", "-")
