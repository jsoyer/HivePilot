"""Tests for hivepilot.ui.plugin_manager — skipped when textual is not installed.

v1 (Phase 26a) was READ-ONLY: browse + inspect loaded plugins, no
enable/disable. Sprint 5 (Phase 26b) adds a `space` toggle that flips a
plugin's presence in `plugins_disabled` and persists it — the browse/inspect
behavior itself is otherwise untouched. Mirrors `tests/test_dashboard.py`'s
`pytest.importorskip("textual.app")` pattern since `textual` is an optional
dep (extras `dashboard`/`full`).
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
    name, source, status, type_label, detail = rows[0]
    assert name == "rtk"
    assert source == "local-file"
    assert status == "enabled"
    assert "runner" in type_label
    assert "rtk" in detail


def test_plugin_rows_attributes_notifier_and_hook_by_module_hint() -> None:
    record = PluginRecord(
        name="obsidian", source="local-file", location="/repo/plugins/obsidian.py"
    )
    notifier_fn = _module_tagged("notify", "hivepilot_plugin_obsidian")
    hook_fn = _module_tagged("on_pipeline_end", "hivepilot_plugin_obsidian")

    rows = plugin_rows([record], {}, {"obsidian": notifier_fn}, {"on_pipeline_end": [hook_fn]})

    name, source, status, type_label, detail = rows[0]
    assert "notifier" in type_label
    assert "hook" in type_label
    assert "obsidian" in detail
    assert "on_pipeline_end" in detail


def test_plugin_rows_attributes_secrets_backend_by_module_hint() -> None:
    """A secrets-backend plugin (infisical / onepassword) must show 'secrets'
    in the Type column — previously it fell through to 'unknown (see
    aggregate)' because plugin_capabilities never cross-referenced SECRETS_MAP.
    The backend is registered as an INSTANCE, so its module is read off its
    class (same as the CLI `plugins list` cross-reference)."""

    class _Backend:
        pass

    _Backend.__module__ = "hivepilot_plugin_infisical"
    backend = _Backend()

    record = PluginRecord(
        name="infisical", source="local-file", location="/repo/plugins/infisical.py"
    )

    rows = plugin_rows([record], {}, {}, {}, secrets_map={"infisical": backend})

    name, source, status, type_label, detail = rows[0]
    assert "secrets" in type_label
    assert type_label != "unknown (see aggregate)"
    assert "infisical" in detail


def test_plugin_rows_attributes_panel_by_module_hint() -> None:
    """A panel-contributing plugin (e.g. sample's `sample_stats` panel) must
    show 'panel' in the Type column — previously 'unknown (see aggregate)'
    because plugin_capabilities never cross-referenced the panels registry.
    The panel is matched by its `fetch` callable's module."""
    fetch = _module_tagged("fetch", "hivepilot_plugin_sample")
    panel_spec = {"name": "sample_stats", "title": "Sample", "fetch": fetch, "min_role": "read"}

    record = PluginRecord(name="sample", source="local-file", location="/repo/plugins/sample.py")

    rows = plugin_rows([record], {}, {}, {}, panels={"sample_stats": panel_spec})

    name, source, status, type_label, detail = rows[0]
    assert "panel" in type_label
    assert type_label != "unknown (see aggregate)"
    assert "sample_stats" in detail


def test_plugin_rows_falls_back_to_unknown_when_attribution_unavailable() -> None:
    """If neither real attribution (`record.contributions`, empty here) nor
    the module-hint fallback can be derived, show the noted placeholder —
    documented in docs/v4/PLUGINS.md."""
    record = PluginRecord(
        name="mystery", source="entry-point", location="unrelated_pkg:register (dist==1.0)"
    )

    rows = plugin_rows([record], {}, {}, {})

    name, source, status, type_label, detail = rows[0]
    assert type_label == "unknown (see aggregate)"


def test_plugin_rows_empty_when_no_plugins_loaded() -> None:
    assert plugin_rows([], {}, {}, {}) == []


class TestRealPerPluginAttribution:
    """Phase 26a: `plugin_capabilities` prefers real `PluginRecord.contributions`
    over the module-hint heuristic when it's populated — no cross-referencing
    the process-global maps needed at all in that case."""

    def test_prefers_record_contributions_over_module_hint(self) -> None:
        from hivepilot.ui.plugin_manager import plugin_capabilities

        # A module hint that would resolve to nothing (no matching entries in
        # any of the maps below) — proves the real `contributions` data, not
        # the fallback, is what actually produced the result.
        record = PluginRecord(
            name="hugo",
            source="local-file",
            location="/repo/plugins/hugo.py",
            contributions={"runners": ["hugo"], "health": ["hugo"]},
        )

        caps = plugin_capabilities(record, {}, {}, {})

        assert caps["runners"] == ["hugo"]
        # "health" isn't one of the TUI's tracked capability kinds
        # (_CAPABILITY_KINDS) — it's silently dropped, same as any other
        # contribution type the TUI doesn't render a column for.
        assert "health" not in caps

    def test_falls_back_to_module_hint_when_contributions_empty(self) -> None:
        """A hand-built `PluginRecord` with no `contributions` (the default)
        still gets the original best-effort module-hint attribution — full
        backward compatibility with every pre-Phase-26a fixture/test."""
        from hivepilot.ui.plugin_manager import plugin_capabilities

        runner_cls = _module_tagged("RtkRunner", "hivepilot_plugin_rtk")
        record = PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py")

        caps = plugin_capabilities(record, {"rtk": runner_cls}, {}, {})

        assert caps["runners"] == ["rtk"]

    def test_explicit_entry_module_hint_matches_entry_point_format(self) -> None:
        """`explicit-entry`'s `location` is a plain `module:attr` string (no
        `(dist==version)` suffix) — the same format `entry-point` uses when
        it has no dist — so the module-hint fallback resolves it the same
        way when `contributions` is empty."""
        from hivepilot.ui.plugin_manager import plugin_capabilities

        runner_cls = _module_tagged("PinnedRunner", "my_pkg.plugin")
        record = PluginRecord(
            name="my_pkg.plugin:register",
            source="explicit-entry",
            location="my_pkg.plugin:register",
        )

        caps = plugin_capabilities(record, {"pinned-kind": runner_cls}, {}, {})

        assert caps["runners"] == ["pinned-kind"]


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
async def test_app_details_surfaces_health_status() -> None:
    """Sprint 2 (plugin-health): the details pane shows the selected plugin's
    health status/detail when a health check is registered under the SAME
    name as the plugin (the convention the example plugins follow)."""
    from hivepilot.plugins import HealthStatus

    record = PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py")
    app = PluginManagerApp(
        loaded=[record],
        runner_map={},
        notifier_map={},
        hooks={},
        health={"rtk": HealthStatus("degraded", "rtk not on PATH")},
    )

    async with app.run_test() as pilot:
        await pilot.press("enter")
        rendered = str(app.details.renderable)
        assert "Health:" in rendered
        assert "degraded" in rendered
        assert "rtk not on PATH" in rendered


@pytest.mark.asyncio
async def test_app_details_no_health_line_when_no_check_registered() -> None:
    record = PluginRecord(name="sample", source="local-file", location="/repo/plugins/sample.py")
    app = PluginManagerApp(loaded=[record], runner_map={}, notifier_map={}, hooks={}, health={})

    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert "Health:" not in str(app.details.renderable)


@pytest.mark.asyncio
async def test_app_surfaces_secrets_type_for_injected_secrets_plugin() -> None:
    """End-to-end through the App: an injected secrets-backend plugin shows
    'secrets' in its Type column instead of 'unknown (see aggregate)'."""

    class _Backend:
        pass

    _Backend.__module__ = "hivepilot_plugin_onepassword"

    record = PluginRecord(
        name="onepassword", source="local-file", location="/repo/plugins/onepassword.py"
    )
    app = PluginManagerApp(
        loaded=[record],
        runner_map={},
        notifier_map={},
        hooks={},
        secrets_map={"onepassword": _Backend()},
    )

    async with app.run_test():
        row = app.plugins_table.get_row_at(0)
        assert "secrets" in row[3]
        assert row[3] != "unknown (see aggregate)"


@pytest.mark.asyncio
async def test_app_surfaces_panel_type_for_injected_panel_plugin() -> None:
    """End-to-end through the App: an injected panel-contributing plugin shows
    'panel' in its Type column instead of 'unknown (see aggregate)'."""
    fetch = _module_tagged("fetch", "hivepilot_plugin_sample")
    record = PluginRecord(name="sample", source="local-file", location="/repo/plugins/sample.py")
    app = PluginManagerApp(
        loaded=[record],
        runner_map={},
        notifier_map={},
        hooks={},
        panels={"sample_stats": {"name": "sample_stats", "title": "S", "fetch": fetch}},
    )

    async with app.run_test():
        row = app.plugins_table.get_row_at(0)
        assert "panel" in row[3]


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


# ---------------------------------------------------------------------------
# Sprint 5 — enable/disable toggle (`space`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_toggle_disables_highlighted_plugin_and_persists(monkeypatch) -> None:
    from hivepilot.config import settings
    from hivepilot.ui import plugin_manager as pm_mod

    monkeypatch.setattr(settings, "plugins_disabled", [], raising=False)
    persist_mock = MagicMock()
    monkeypatch.setattr(pm_mod, "persist_plugins_disabled", persist_mock)

    record = PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py")
    app = PluginManagerApp(loaded=[record], runner_map={}, notifier_map={}, hooks={})

    async with app.run_test() as pilot:
        await pilot.press("space")

        assert "rtk" in settings.plugins_disabled
        persist_mock.assert_called_once_with(["rtk"])
        # Status cell reflects the change immediately.
        row = app.plugins_table.get_row_at(0)
        assert row[2] == "disabled"
        assert "next start" in str(app.details.renderable)


@pytest.mark.asyncio
async def test_action_toggle_re_enables_a_disabled_plugin(monkeypatch) -> None:
    from hivepilot.config import settings
    from hivepilot.ui import plugin_manager as pm_mod

    monkeypatch.setattr(settings, "plugins_disabled", ["rtk"], raising=False)
    persist_mock = MagicMock()
    monkeypatch.setattr(pm_mod, "persist_plugins_disabled", persist_mock)

    record = PluginRecord(name="rtk", source="local-file", location="/repo/plugins/rtk.py")
    app = PluginManagerApp(loaded=[record], runner_map={}, notifier_map={}, hooks={})

    async with app.run_test() as pilot:
        await pilot.press("space")

        assert "rtk" not in settings.plugins_disabled
        persist_mock.assert_called_once_with([])
        row = app.plugins_table.get_row_at(0)
        assert row[2] == "enabled"


@pytest.mark.asyncio
async def test_action_toggle_noop_when_no_plugins_loaded(monkeypatch) -> None:
    from hivepilot.ui import plugin_manager as pm_mod

    persist_mock = MagicMock()
    monkeypatch.setattr(pm_mod, "persist_plugins_disabled", persist_mock)

    app = PluginManagerApp(loaded=[], runner_map={}, notifier_map={}, hooks={})

    async with app.run_test() as pilot:
        await pilot.press("space")

        persist_mock.assert_not_called()


def test_persist_plugins_disabled_upserts_env_file(tmp_path) -> None:
    """The persist writer upserts HIVEPILOT_PLUGINS_DISABLED into the .env
    file Settings reads — preserving unrelated lines verbatim."""
    from hivepilot.ui.plugin_manager import persist_plugins_disabled

    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_OTHER=keep-me\n", encoding="utf-8")

    persist_plugins_disabled(["rtk", "obsidian"], env_path=env_path)

    content = env_path.read_text(encoding="utf-8")
    assert "HIVEPILOT_OTHER=keep-me" in content
    # persist_plugins_disabled sorts before writing (deterministic diffs).
    assert 'HIVEPILOT_PLUGINS_DISABLED=["obsidian", "rtk"]' in content

    # A second call replaces the existing line rather than duplicating it.
    persist_plugins_disabled(["rtk"], env_path=env_path)
    content = env_path.read_text(encoding="utf-8")
    assert content.count("HIVEPILOT_PLUGINS_DISABLED=") == 1
    assert 'HIVEPILOT_PLUGINS_DISABLED=["rtk"]' in content


class TestPersistPluginsDisabledRoundTrips:
    """Prove the persisted format is actually CONSUMABLE by Settings, not
    merely well-formed JSON — i.e. python-dotenv + pydantic-settings really
    deserialize the unquoted-JSON `.env` line back into `plugins_disabled`.
    A merely-well-formed-JSON assertion (as in the test above) would pass
    even if Settings couldn't parse it back (e.g. if dotenv quoting stripped
    or mangled the value) — this is the behavioral check."""

    def test_single_entry_round_trips_through_settings(self, tmp_path) -> None:
        from hivepilot.config import Settings
        from hivepilot.ui.plugin_manager import persist_plugins_disabled

        env_path = tmp_path / ".env"
        persist_plugins_disabled(["rtk"], env_path=env_path)

        s = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
        assert s.plugins_disabled == ["rtk"]

    def test_two_entries_round_trip_through_settings(self, tmp_path) -> None:
        from hivepilot.config import Settings
        from hivepilot.ui.plugin_manager import persist_plugins_disabled

        env_path = tmp_path / ".env"
        persist_plugins_disabled(["rtk", "obsidian"], env_path=env_path)

        s = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
        assert s.plugins_disabled == ["obsidian", "rtk"]
