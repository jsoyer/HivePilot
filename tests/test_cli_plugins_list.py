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
from hivepilot.plugins import PluginRecord
from hivepilot.registry import RUNNER_MAP
from hivepilot.services.notification_service import NOTIFIER_MAP


def test_plugins_list_exits_zero_and_lists_builtins(monkeypatch) -> None:
    """With no plugins loaded, `plugins list` still exits 0 and lists every
    built-in runner kind and notifier currently registered."""
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
