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


def _cell_plain(value: Any) -> str:
    """Normalize a DataTable cell to plain text — cells may be a raw `str` or
    a `rich.text.Text` (panel `stat`/`table` sections and the Health tab's
    colored status badge are all rendered as `Text`; see
    `dashboard._panel_stat_widget`/`_panel_table_widget`/`_health_status_cell`).
    Mirrors `tests/test_dashboard.py::_cell_plain`."""
    return value.plain if hasattr(value, "plain") else str(value)


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
        table_rows = [
            [_cell_plain(cell) for cell in tables[0].get_row_at(r)]
            for r in range(tables[0].row_count)
        ]
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
async def test_panel_stat_untrusted_markup_is_rendered_plain_not_as_markup() -> None:
    """A `stat` section's `label`/`value` that looks like Rich/Textual markup
    must also render literally — same guarantee `_panel_stat_widget` gives
    `text` sections, exercised here for the `stat` kind specifically."""
    markup_label = "[red]label[/red]"
    markup_value = "[bold]value[/bold]"

    def _fetch() -> PanelData:
        return PanelData(
            sections=[
                {"kind": "stat", "label": markup_label, "value": markup_value, "status": None}
            ]
        )

    manager = _FakePluginManager(
        panels=[_panel_spec("stat_markup", "Stat Markup Panel")], fetchers={"stat_markup": _fetch}
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        await app.refresh_panel("stat_markup")

        from textual.widgets import Static

        container = app.query_one("#panel-stat_markup-content")
        statics = list(container.query(Static))
        rendered_texts = [str(getattr(s.renderable, "plain", s.renderable)) for s in statics]
        combined = " ".join(rendered_texts)

        assert markup_label in combined
        assert markup_value in combined


@pytest.mark.asyncio
async def test_panel_table_untrusted_markup_is_rendered_plain_not_as_markup() -> None:
    """A `table` section's column headers and cells that look like
    Rich/Textual markup must render literally — `_panel_table_widget` wraps
    each header/cell in `rich.text.Text(...)`, same guarantee as the `stat`
    and `text` sections."""
    markup_column = "[red]col[/red]"
    markup_cell = "[bold]cell[/bold]"

    def _fetch() -> PanelData:
        return PanelData(
            sections=[
                {
                    "kind": "table",
                    "columns": [markup_column, "plain"],
                    "rows": [[markup_cell, "ok"]],
                }
            ]
        )

    manager = _FakePluginManager(
        panels=[_panel_spec("table_markup", "Table Markup Panel")],
        fetchers={"table_markup": _fetch},
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test():
        await app.refresh_panel("table_markup")

        from textual.widgets import DataTable

        container = app.query_one("#panel-table_markup-content")
        tables = list(container.query(DataTable))
        assert len(tables) == 1
        table = tables[0]

        column_labels = [_cell_plain(col.label) for col in table.columns.values()]
        assert markup_column in column_labels

        row_cells = [_cell_plain(cell) for cell in table.get_row_at(0)]
        assert markup_cell in row_cells


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
async def test_switching_to_panel_tab_fetches_and_does_not_duplicate_rows_on_reactivation() -> None:
    """Real-UI integration test for the fetch-on-activate design: switches
    tabs the way a user actually does (`TabbedContent.active = <pane id>`,
    which drives the exact same `Tabs`/`ContentSwitcher` machinery a
    `pilot.click` on the tab bar would — see `TabbedContent._watch_active`),
    rather than calling `app.refresh_panel(name)` directly like every other
    panel test in this module.

    Asserts (a) the first activation fetches and renders the panel's table
    section, and (b) activating the SAME tab a second time re-fetches
    (`refresh_panel` rebuilds via `container.remove_children()` then
    `mount()`) without appending duplicate rows — the key correctness proof
    that `on_tabbed_content_tab_activated` -> `refresh_panel` replaces
    content rather than accumulating it.
    """
    fetch_calls = 0

    def _fetch() -> PanelData:
        nonlocal fetch_calls
        fetch_calls += 1
        return PanelData(
            sections=[
                {
                    "kind": "table",
                    "columns": ["project", "status"],
                    "rows": [["demo", "ok"], ["acme", "ok"]],
                }
            ]
        )

    manager = _FakePluginManager(
        panels=[_panel_spec("activation", "Activation Panel")], fetchers={"activation": _fetch}
    )
    app = RunDashboard(plugin_manager=manager)
    async with app.run_test() as pilot:
        from textual.widgets import DataTable, TabbedContent

        tabbed = app.query_one(TabbedContent)
        pane_id = "panel-activation-tab"

        # First activation: real UI tab switch, not a direct refresh_panel() call.
        tabbed.active = pane_id
        await pilot.pause()

        container = app.query_one("#panel-activation-content")
        tables = list(container.query(DataTable))
        assert len(tables) == 1
        assert tables[0].row_count == 2
        assert fetch_calls == 1

        # Switch away, then back to the same panel tab a second time.
        tabbed.active = "analytics-tab"
        await pilot.pause()
        tabbed.active = pane_id
        await pilot.pause()

        tables_after = list(container.query(DataTable))
        assert len(tables_after) == 1
        assert tables_after[0].row_count == 2  # not duplicated (would be 4 if appended)
        assert fetch_calls == 2  # re-fetched on the second activation, not cached


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
