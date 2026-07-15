"""
Tests for Sprint 4 (plugin system) — `hivepilot plugins list` CLI command.

Covers:
- the command exits 0 and lists the built-in runner kinds and notifiers
- a loaded `PluginRecord` (name/source/location) is listed when present

v1 simplification (see docs/v4/PLUGINS.md): this is an inventory (what's
loaded, from where) plus a separate list of what runner kinds / notifier
names are currently registered (built-in vs. plugin, inferred by membership)
— not a full join between the two.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.models import KNOWN_RUNNER_KINDS
from hivepilot.plugins import HealthStatus, PluginRecord
from hivepilot.registry import KNOWN_SECRET_BACKENDS, RUNNER_MAP, SECRETS_MAP, SecretsRegistry
from hivepilot.services.notification_service import NOTIFIER_MAP


class TestPluginsListHealthTable:
    """Sprint 2 (plugin-health): `plugins list` gains a Health table, sourced
    from `PluginManager.check_all()` (never-raise — see `hivepilot/plugins.py`).
    """

    def test_plugins_list_renders_health_table(self, monkeypatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.loaded = []
        mock_orch.plugins.check_all.return_value = {
            "rtk": HealthStatus("ok", "rtk on PATH"),
            "obsidian": HealthStatus("degraded", "not configured"),
        }
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "rtk" in result.output
        assert "ok" in result.output
        assert "rtk on PATH" in result.output
        assert "obsidian" in result.output
        assert "degraded" in result.output
        assert "not configured" in result.output

    def test_plugins_list_health_table_placeholder_when_no_checks(self, monkeypatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.loaded = []
        mock_orch.plugins.check_all.return_value = {}
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "Health" in result.output


class TestPluginsHealthCommand:
    """`hivepilot plugins health` — focused health-only command with a
    monitoring-friendly non-zero exit code when any check reports `error`."""

    def test_exits_zero_when_all_ok(self, monkeypatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.check_all.return_value = {
            "rtk": HealthStatus("ok", "rtk on PATH"),
        }
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "health"])

        assert result.exit_code == 0, result.output
        assert "rtk" in result.output

    def test_exits_nonzero_when_any_check_errors(self, monkeypatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.check_all.return_value = {
            "rtk": HealthStatus("ok", "rtk on PATH"),
            "mem0": HealthStatus("error", "lib missing"),
        }
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "health"])

        assert result.exit_code != 0
        assert "mem0" in result.output
        assert "lib missing" in result.output

    def test_exits_zero_when_no_checks_registered(self, monkeypatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.check_all.return_value = {}
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "health"])

        assert result.exit_code == 0, result.output


def test_plugins_list_exits_zero_and_lists_builtins(monkeypatch) -> None:
    """With no plugins loaded, `plugins list` still exits 0 and lists every
    built-in runner kind, notifier, and secrets backend currently registered."""
    mock_orch = MagicMock()
    mock_orch.plugins.loaded = []

    monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0, result.output

    for kind in RUNNER_MAP:
        assert kind in KNOWN_RUNNER_KINDS  # sanity: every registered kind is a known built-in here
        assert kind in result.output, f"runner kind {kind!r} missing from output"

    for name in NOTIFIER_MAP:
        assert name in result.output, f"notifier {name!r} missing from output"

    for name in SECRETS_MAP:
        assert name in KNOWN_SECRET_BACKENDS  # sanity: every registered backend is builtin here
        assert name in result.output, f"secrets backend {name!r} missing from output"


def test_plugins_list_secrets_table_includes_plugin_backend(monkeypatch) -> None:
    """A plugin-contributed secrets backend (not one of the four builtins)
    shows up in the Secrets Backends table, distinct from the builtins."""

    class _PluginBackend:
        def resolve(self, ref, settings):  # noqa: ANN001
            return "plugin-value"

    SecretsRegistry.register("my-plugin-backend", _PluginBackend())
    try:
        mock_orch = MagicMock()
        mock_orch.plugins.loaded = []
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "my-plugin-backend" in result.output
        for name in KNOWN_SECRET_BACKENDS:
            assert name in result.output
    finally:
        SECRETS_MAP.pop("my-plugin-backend", None)


def test_plugins_list_includes_loaded_plugin_record(monkeypatch) -> None:
    """A loaded `PluginRecord` (name/source/location) is present in the output."""
    record = PluginRecord(name="my_plugin", source="local-file", location="plugins/my_plugin.py")
    mock_orch = MagicMock()
    mock_orch.plugins.loaded = [record]

    monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0, result.output
    assert "my_plugin" in result.output
    assert "local-file" in result.output
    assert "plugins/my_plugin.py" in result.output
