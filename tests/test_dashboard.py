"""Tests for hivepilot.ui.dashboard — skipped when textual is not installed."""

from __future__ import annotations

from typing import Any

import pytest

textual = pytest.importorskip("textual.app")

from textual.coordinate import Coordinate  # noqa: E402

from hivepilot.plugins import HealthStatus  # noqa: E402
from hivepilot.ui.dashboard import RunDashboard, _load_mem0_plugin_module  # noqa: E402


def _cell_plain(value: Any) -> str:
    """Normalize a DataTable cell to plain text — cells may be a raw `str`
    (most tables) or a `rich.text.Text` (the Health tab's colored status
    badge, see `dashboard._health_status_cell`)."""
    return value.plain if hasattr(value, "plain") else str(value)


def test_refresh_interactions_method_exists() -> None:
    assert hasattr(RunDashboard, "refresh_interactions")


def test_refresh_interactions_is_callable() -> None:
    assert callable(getattr(RunDashboard, "refresh_interactions"))


def test_title_is_mirador() -> None:
    assert RunDashboard.TITLE == "Mirador"


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
# Mirador tabbed layout — Sprint: Analytics / Cost / Health / Mem0 tabs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_has_all_tabbed_tables() -> None:
    """Every tab's table (+ the Mem0 status label) exists after mount, and
    the pre-existing tables still populate (regression: the tabbed layout
    must not drop any of the folded-in Analytics tables)."""
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
        assert isinstance(app.mem0_table, DataTable)


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
    mem0/rtk/obsidian — are all fast/local, gated behind `*_enabled`
    settings that default False, so this never makes a network call in
    CI). Every rendered status must be one of the three valid values."""
    app = RunDashboard()
    async with app.run_test():
        for r in range(app.health_table.row_count):
            status = _cell_plain(app.health_table.get_cell_at(Coordinate(r, 1)))
            assert status in ("ok", "degraded", "error", "-")


# ---------------------------------------------------------------------------
# Mem0 tab
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_mem0_shows_not_configured_placeholder_when_disabled() -> None:
    """mem0 unconfigured (settings.mem0_enabled defaults False) -> a clear
    "not configured" placeholder renders, table stays empty, no crash. Uses
    the REAL `plugins/mem0.py` file (found via `settings.base_dir`, patched
    to the repo root by conftest's session-scoped `_isolate_config_resolution`
    fixture) — no mocking needed for the default/unconfigured path."""
    app = RunDashboard()
    async with app.run_test():
        assert app.mem0_table.row_count == 0
        status_text = str(app.mem0_status.renderable)
        assert "not configured" in status_text.lower()


class _FakeMem0Client:
    def __init__(self, memories: list[dict[str, object]]) -> None:
        self._memories = memories

    def get_all(self) -> list[dict[str, object]]:
        return self._memories


class _FakeMem0Module:
    def __init__(self, client: _FakeMem0Client) -> None:
        self._client = client

    def _get_client(self) -> _FakeMem0Client:
        return self._client


@pytest.mark.asyncio
async def test_refresh_mem0_lists_memories_when_configured(monkeypatch) -> None:
    """mem0 configured + a client that can list memories -> the table shows
    the typed provenance metadata (category/project/task/ts) alongside the
    memory text, and the status label reports the count."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)

    memories = [
        {
            "memory": "prefers concise commit messages",
            "metadata": {
                "category": "run",
                "project": "acme",
                "task": "task1",
                "ts": "2026-07-16T00:00:00+00:00",
            },
        }
    ]
    fake_module = _FakeMem0Module(_FakeMem0Client(memories))

    app = RunDashboard(mem0_module=fake_module)
    async with app.run_test():
        assert app.mem0_table.row_count == 1
        assert _cell_plain(app.mem0_table.get_cell_at(Coordinate(0, 0))) == "run"
        assert _cell_plain(app.mem0_table.get_cell_at(Coordinate(0, 1))) == "acme"
        assert _cell_plain(app.mem0_table.get_cell_at(Coordinate(0, 2))) == "task1"
        assert (
            _cell_plain(app.mem0_table.get_cell_at(Coordinate(0, 3))) == "2026-07-16T00:00:00+00:00"
        )
        assert "concise commit messages" in _cell_plain(
            app.mem0_table.get_cell_at(Coordinate(0, 4))
        )
        assert "1 recent memor" in str(app.mem0_status.renderable)


@pytest.mark.asyncio
async def test_refresh_mem0_no_secret_leaked_in_status_or_table(monkeypatch) -> None:
    """A client whose `get_all()` raises must degrade to a status message
    containing only the exception TYPE name — never `str(exc)` (which could
    echo back a token/URL/config value)."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)

    class _RaisingClient:
        def get_all(self) -> list[dict[str, object]]:
            raise RuntimeError("secret-token=abc123 leaked-in-exception-message")

    fake_module = _FakeMem0Module(_RaisingClient())  # type: ignore[arg-type]

    app = RunDashboard(mem0_module=fake_module)
    async with app.run_test():
        assert app.mem0_table.row_count == 0
        status_text = str(app.mem0_status.renderable)
        assert "secret-token" not in status_text
        assert "RuntimeError" in status_text


def test_load_mem0_plugin_module_honors_kill_switches(monkeypatch) -> None:
    """The Mem0 tab's loader must respect the plugin-system kill switches
    exactly like `hivepilot.plugins._scan_local_plugins`: a globally-disabled
    plugin system, or `mem0` in `plugins_disabled`, means the module is never
    loaded (so no live mem0 backend call happens) — even though
    `plugins/mem0.py` exists on disk.
    """
    from hivepilot.config import settings

    # Sanity: with plugins enabled and mem0 not disabled, the real
    # plugins/mem0.py IS loaded (proves the None results below are caused by
    # the kill switches, not by the file being absent / a load error).
    monkeypatch.setattr(settings, "plugins_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plugins_disabled", [], raising=False)
    assert _load_mem0_plugin_module() is not None

    # Global kill switch off -> never load.
    monkeypatch.setattr(settings, "plugins_enabled", False, raising=False)
    assert _load_mem0_plugin_module() is None

    # Per-plugin disable -> never load, even with the system enabled.
    monkeypatch.setattr(settings, "plugins_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plugins_disabled", ["mem0"], raising=False)
    assert _load_mem0_plugin_module() is None
