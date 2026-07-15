"""Tests for hivepilot.ui.plugin_manager — skipped when textual is not installed.

v1 is READ-ONLY: browse + inspect loaded plugins, no enable/disable. Mirrors
`tests/test_dashboard.py`'s `pytest.importorskip("textual.app")` pattern since
`textual` is an optional dep (extras `dashboard`/`full`).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

textual = pytest.importorskip("textual.app")

from hivepilot.plugins import PluginRecord  # noqa: E402
from hivepilot.ui.plugin_manager import PluginManagerApp, plugin_rows  # noqa: E402


def _module_tagged(name: str, module: str):
    """Build a bare object whose __module__ mimics a loaded plugin's module name."""

    class _Obj:
        pass

    _Obj.__name__ = name
    _Obj.__module__ = module
    return _Obj


def test_plugin_rows_attributes_local_file_runner_by_module_hint() -> None:
    record = PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py")
    runner_cls = _module_tagged("RtkRunner", "hivepilot_plugin_rtk")

    rows = plugin_rows([record], {"rtk": runner_cls}, {}, {})

    assert len(rows) == 1
    name, source, type_label, detail = rows[0]
    assert name == "rtk"
    assert source == "local-file"
    assert "runner" in type_label
    assert "rtk" in detail


def test_plugin_rows_attributes_notifier_and_hook_by_module_hint() -> None:
    record = PluginRecord(
        name="obsidian", source="local-file", location="/repo/plugins/obsidian.py"
    )
    notifier_fn = _module_tagged("notify", "hivepilot_plugin_obsidian")
    hook_fn = _module_tagged("on_pipeline_end", "hivepilot_plugin_obsidian")

    rows = plugin_rows([record], {}, {"obsidian": notifier_fn}, {"on_pipeline_end": [hook_fn]})

    name, source, type_label, detail = rows[0]
    assert "notifier" in type_label
    assert "hook" in type_label
    assert "obsidian" in detail
    assert "on_pipeline_end" in detail


def test_plugin_rows_falls_back_to_unknown_when_attribution_unavailable() -> None:
    """If per-plugin attribution can't be derived, show the (noted) fallback —
    matches the roadmap Phase 26a limitation documented in docs/v4/PLUGINS.md."""
    record = PluginRecord(
        name="mystery", source="entry-point", location="unrelated_pkg:register (dist==1.0)"
    )

    rows = plugin_rows([record], {}, {}, {})

    name, source, type_label, detail = rows[0]
    assert type_label == "unknown (see aggregate)"


def test_plugin_rows_empty_when_no_plugins_loaded() -> None:
    assert plugin_rows([], {}, {}, {}) == []


@pytest.mark.asyncio
async def test_app_populates_data_table_with_injected_plugins() -> None:
    record = PluginRecord(name="sample", source="local-file", location="/repo/plugins/sample.py")
    app = PluginManagerApp(loaded=[record], runner_map={}, notifier_map={}, hooks={})

    async with app.run_test():
        table = app.plugins_table
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "sample"
        assert row[1] == "local-file"


@pytest.mark.asyncio
async def test_app_shows_multiple_loaded_plugins() -> None:
    records = [
        PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py"),
        PluginRecord(name="obsidian", source="local-file", location="/repo/plugins/obsidian.py"),
        PluginRecord(name="headroom", source="local-file", location="/repo/plugins/headroom.py"),
        PluginRecord(name="sample", source="local-file", location="/repo/plugins/sample.py"),
    ]
    app = PluginManagerApp(loaded=records, runner_map={}, notifier_map={}, hooks={})

    async with app.run_test():
        table = app.plugins_table
        assert table.row_count == 4
        names = {table.get_row_at(i)[0] for i in range(table.row_count)}
        assert names == {"rtk", "obsidian", "headroom", "sample"}


@pytest.mark.asyncio
async def test_app_details_update_on_enter() -> None:
    record = PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py")
    runner_cls = _module_tagged("RtkRunner", "hivepilot_plugin_rtk")
    app = PluginManagerApp(
        loaded=[record], runner_map={"rtk": runner_cls}, notifier_map={}, hooks={}
    )

    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert "rtk" in str(app.details.renderable)


@pytest.mark.asyncio
async def test_app_details_default_message_when_no_plugins() -> None:
    app = PluginManagerApp(loaded=[], runner_map={}, notifier_map={}, hooks={})

    async with app.run_test():
        assert app.plugins_table.row_count == 0
        assert "No plugins" in str(app.details.renderable)


def test_plugins_tui_prints_message_and_exits_when_disabled(monkeypatch) -> None:
    from typer.testing import CliRunner

    from hivepilot.cli import app as cli_app
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "enable_textual_ui", False, raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["plugins", "tui"])

    assert result.exit_code != 0
    assert "HIVEPILOT_ENABLE_TEXTUAL_UI" in result.output


def test_plugins_tui_launches_app_when_enabled(monkeypatch) -> None:
    from typer.testing import CliRunner

    from hivepilot.cli import app as cli_app
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "enable_textual_ui", True, raising=False)
    mock_run = MagicMock()
    monkeypatch.setattr("hivepilot.ui.plugin_manager.PluginManagerApp.run", mock_run)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["plugins", "tui"])

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
