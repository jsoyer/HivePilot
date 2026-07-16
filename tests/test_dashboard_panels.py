"""Tests for Mirador panel-plugin TUI rendering (Sprint 2).

Covers: dynamic per-panel tabs built from `PluginManager.list_panels()`,
generic section rendering (stat/table/text), the never-raise + no-secret
guarantee (reusing `run_panel_fetch`'s already-normalized error panel),
untrusted-text-as-plain-text handling, a zero-section "no data" placeholder,
graceful degrade to zero panel tabs when the plugin manager is unavailable,
and that the real `plugins/sample.py` panel renders end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

textual = pytest.importorskip("textual.app")

from hivepilot.plugins import PanelData  # noqa: E402
from hivepilot.ui.dashboard import RunDashboard  # noqa: E402


class _FakePluginManager:
    """Minimal stand-in exposing the same two methods the dashboard calls on
    a real `PluginManager`: `list_panels()` and `run_panel_fetch(name)`."""

    def __init__(self, panels: list[dict[str, Any]], fetchers: dict[str, Any]) -> None:
        self._panels = panels
        self._fetchers = fetchers

    def list_panels(self) -> list[dict[str, Any]]:
        return list(self._panels)

    def run_panel_fetch(self, name: str) -> PanelData:
        fn = self._fetchers.get(name)
        if fn is None:
            return PanelData(
                sections=[
                    {"kind": "stat", "label": "error", "value": "PanelNotFound", "status": "error"}
                ]
            )
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — mirrors PluginManager.run_panel_fetch
            return PanelData(
                sections=[
                    {
                        "kind": "stat",
                        "label": "error",
                        "value": type(exc).__name__,
                        "status": "error",
                    }
                ]
            )


def _panel_spec(name: str, title: str) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "fetch": lambda: PanelData(sections=[]),
        "min_role": "read",
    }


@pytest.mark.asyncio
async def test_orchestrator_construction_failure_degrades_to_no_panels(monkeypatch) -> None:
    from hivepilot.ui import dashboard as dashboard_mod

    def _boom() -> Any:
        raise RuntimeError("orchestrator init failed")

    monkeypatch.setattr(dashboard_mod, "Orchestrator", _boom)

    app = RunDashboard()
    async with app.run_test():
        assert app._panels == []
        assert app.metrics_table is not None


@pytest.mark.asyncio
async def test_panel_tabs_appear_with_correct_titles() -> None:
    manager = _FakePluginManager(
        panels=[_panel_spec("alpha", "Alpha Panel"), _panel_spec("beta", "Beta Panel")],
        fetchers={},
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test() as pilot:
        from textual.widgets import TabPane

        tab_pane_ids = {pane.id for pane in app.query(TabPane)}
        assert any("alpha" in (tid or "") for tid in tab_pane_ids)
        assert any("beta" in (tid or "") for tid in tab_pane_ids)
        titles = {str(pane._title) for pane in app.query(TabPane)}
        assert "Alpha Panel" in titles
        assert "Beta Panel" in titles
        await pilot.pause()


@pytest.mark.asyncio
async def test_panel_with_stat_table_text_sections_renders_each() -> None:
    def _fetch() -> PanelData:
        return PanelData(
            sections=[
                {"kind": "stat", "label": "steps run", "value": "42", "status": "ok"},
                {
                    "kind": "table",
                    "columns": ["project", "status"],
                    "rows": [["demo", "ok"]],
                },
                {"kind": "text", "content": "hello panel"},
            ]
        )

    manager = _FakePluginManager(panels=[_panel_spec("mix", "Mix Panel")], fetchers={"mix": _fetch})
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        await app.refresh_panel("mix")

        from textual.widgets import DataTable, Static

        container = app.query_one("#panel-mix-content")
        statics = list(container.query(Static))
        tables = list(container.query(DataTable))

        assert len(tables) == 1
        assert tables[0].columns
        table_rows = [tables[0].get_row_at(r) for r in range(tables[0].row_count)]
        assert ["demo", "ok"] in table_rows

        rendered_texts = [str(getattr(s.renderable, "plain", s.renderable)) for s in statics]
        assert any("steps run" in t and "42" in t for t in rendered_texts)
        assert any("hello panel" in t for t in rendered_texts)


@pytest.mark.asyncio
async def test_panel_raising_fetch_renders_error_panel_with_only_exception_type() -> None:
    secret = "super-secret-token-abc123"

    def _fetch() -> PanelData:
        raise RuntimeError(secret)

    manager = _FakePluginManager(
        panels=[_panel_spec("boom", "Boom Panel")], fetchers={"boom": _fetch}
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        await app.refresh_panel("boom")

        from textual.widgets import Static

        container = app.query_one("#panel-boom-content")
        statics = list(container.query(Static))
        rendered_texts = [str(getattr(s.renderable, "plain", s.renderable)) for s in statics]
        combined = " ".join(rendered_texts)

        assert secret not in combined
        assert "RuntimeError" in combined


@pytest.mark.asyncio
async def test_panel_untrusted_text_is_rendered_plain_not_as_markup() -> None:
    """A text section whose content looks like Rich/Textual markup
    (`[bold red]...[/]`) must render literally, never interpreted as a
    style tag."""
    markup_like = "[bold red]not a real style[/bold red]"

    def _fetch() -> PanelData:
        return PanelData(sections=[{"kind": "text", "content": markup_like}])

    manager = _FakePluginManager(
        panels=[_panel_spec("untrusted", "Untrusted Panel")], fetchers={"untrusted": _fetch}
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        await app.refresh_panel("untrusted")

        from textual.widgets import Static

        container = app.query_one("#panel-untrusted-content")
        statics = list(container.query(Static))
        rendered_texts = [str(getattr(s.renderable, "plain", s.renderable)) for s in statics]
        assert any(markup_like in t for t in rendered_texts)


@pytest.mark.asyncio
async def test_panel_zero_sections_renders_no_data_placeholder() -> None:
    def _fetch() -> PanelData:
        return PanelData(sections=[])

    manager = _FakePluginManager(
        panels=[_panel_spec("empty", "Empty Panel")], fetchers={"empty": _fetch}
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        await app.refresh_panel("empty")

        from textual.widgets import Static

        container = app.query_one("#panel-empty-content")
        statics = list(container.query(Static))
        rendered_texts = [str(getattr(s.renderable, "plain", s.renderable)) for s in statics]
        assert any("no data" in t.lower() for t in rendered_texts)


@pytest.mark.asyncio
async def test_no_panels_registered_builtin_tabs_still_work() -> None:
    manager = _FakePluginManager(panels=[], fetchers={})
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        from textual.widgets import TabPane

        pane_ids = {pane.id for pane in app.query(TabPane)}
        assert pane_ids == {"analytics-tab", "cost-tab", "health-tab", "mem0-tab"}
        assert app.metrics_table is not None


@pytest.mark.asyncio
async def test_real_sample_plugin_panel_renders_end_to_end() -> None:
    """No injected override -> real `Orchestrator().plugins` discovers the
    repo's own `plugins/sample.py` `sample_stats` panel (see
    `tests/test_panels.py::TestSamplePanelIntegration`), and it renders
    through the same generic section-widget path."""
    app = RunDashboard()
    async with app.run_test():
        names = {panel["name"] for panel in app._panels}
        if "sample_stats" not in names:
            pytest.skip("plugins/sample.py not discovered in this environment")

        await app.refresh_panel("sample_stats")

        from textual.widgets import DataTable, Static

        container = app.query_one("#panel-sample_stats-content")
        statics = list(container.query(Static))
        tables = list(container.query(DataTable))
        assert len(tables) == 1
        rendered_texts = [str(getattr(s.renderable, "plain", s.renderable)) for s in statics]
        assert any("steps run" in t and "42" in t for t in rendered_texts)
