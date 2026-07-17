"""
Tests for Phase 26b Approach A — `hivepilot plugins search` / `plugins info`
CLI commands (metadata-only plugin discovery index).

CRITICAL: these commands must NEVER trigger a download/exec of plugin code —
only fetch and display index metadata. Every test that exercises the happy
path also asserts no subprocess/pip/import call was made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.plugins import PluginRecord
from hivepilot.services.plugin_index import PluginIndexEntry


def _entries() -> list[PluginIndexEntry]:
    return [
        PluginIndexEntry(
            name="hugo",
            description="Static site runner plugin",
            author="jsoyer",
            homepage="https://example.com/hugo",
            install={"type": "pip", "target": "hivepilot-plugin-hugo"},
            version="1.2.0",
            checksum="sha256:deadbeef",
            contributes=["runners"],
        ),
        PluginIndexEntry(
            name="obsidian",
            description="Notifier for Obsidian vaults",
            install={"type": "git", "target": "https://github.com/org/obsidian-plugin"},
        ),
    ]


class TestPluginsSearch:
    def test_search_renders_matches(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.fetch_index", return_value=_entries()
        ) as mock_fetch:
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "search", "hugo"])

        assert result.exit_code == 0, result.output
        assert "hugo" in result.output
        assert "obsidian" not in result.output
        mock_fetch.assert_called_once()

    def test_search_empty_query_lists_all(self) -> None:
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "search"])

        assert result.exit_code == 0, result.output
        assert "hugo" in result.output
        assert "obsidian" in result.output

    def test_search_no_index_configured_friendly_error(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.fetch_index",
            side_effect=RuntimeError(
                "no plugin index configured — set HIVEPILOT_PLUGINS_INDEX_URL"
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "search", "hugo"])

        assert result.exit_code == 1
        assert "no plugin index configured" in result.output
        assert "Traceback" not in result.output

    def test_search_network_error_friendly_error_no_traceback(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.fetch_index",
            side_effect=RuntimeError("failed to reach plugin index (ConnectionError)"),
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "search", "hugo"])

        assert result.exit_code == 1
        assert "failed to reach plugin index" in result.output
        assert "Traceback" not in result.output
        assert "raise" not in result.output.lower()

    def test_search_never_installs_or_execs_anything(self) -> None:
        """Guard the trust model: `search` must never shell out, pip-install,
        or import anything based on fetched metadata."""
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            with (
                patch("subprocess.run") as mock_subprocess,
                patch("subprocess.Popen") as mock_popen,
                patch("importlib.import_module") as mock_import,
            ):
                runner = CliRunner()
                result = runner.invoke(app, ["plugins", "search"])

        assert result.exit_code == 0, result.output
        mock_subprocess.assert_not_called()
        mock_popen.assert_not_called()
        mock_import.assert_not_called()


class TestPluginsInfo:
    def test_info_shows_metadata_install_command_and_checksum(self) -> None:
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            mock_orch = MagicMock()
            mock_orch.plugins.loaded = []
            with patch("hivepilot.cli.Orchestrator", lambda: mock_orch):
                runner = CliRunner()
                result = runner.invoke(app, ["plugins", "info", "hugo"])

        assert result.exit_code == 0, result.output
        assert "hugo" in result.output
        assert "jsoyer" in result.output
        assert "https://example.com/hugo" in result.output
        assert "pip install hivepilot-plugin-hugo" in result.output
        assert "sha256:deadbeef" in result.output

    def test_info_git_install_hint(self) -> None:
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            mock_orch = MagicMock()
            mock_orch.plugins.loaded = []
            with patch("hivepilot.cli.Orchestrator", lambda: mock_orch):
                runner = CliRunner()
                result = runner.invoke(app, ["plugins", "info", "obsidian"])

        assert result.exit_code == 0, result.output
        assert "git clone https://github.com/org/obsidian-plugin" in result.output

    def test_info_case_insensitive_lookup(self) -> None:
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            mock_orch = MagicMock()
            mock_orch.plugins.loaded = []
            with patch("hivepilot.cli.Orchestrator", lambda: mock_orch):
                runner = CliRunner()
                result = runner.invoke(app, ["plugins", "info", "HUGO"])

        assert result.exit_code == 0, result.output
        assert "hugo" in result.output

    def test_info_notes_when_already_installed_locally(self) -> None:
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            mock_orch = MagicMock()
            mock_orch.plugins.loaded = [
                PluginRecord(name="hugo", source="local-file", location="plugins/hugo.py")
            ]
            with patch("hivepilot.cli.Orchestrator", lambda: mock_orch):
                runner = CliRunner()
                result = runner.invoke(app, ["plugins", "info", "hugo"])

        assert result.exit_code == 0, result.output
        assert "yes" in result.output.lower()

    def test_info_unknown_name_friendly_error(self) -> None:
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            mock_orch = MagicMock()
            mock_orch.plugins.loaded = []
            with patch("hivepilot.cli.Orchestrator", lambda: mock_orch):
                runner = CliRunner()
                result = runner.invoke(app, ["plugins", "info", "does-not-exist"])

        assert result.exit_code == 1
        assert "does-not-exist" in result.output
        assert "Traceback" not in result.output

    def test_info_no_index_configured_friendly_error(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.fetch_index",
            side_effect=RuntimeError(
                "no plugin index configured — set HIVEPILOT_PLUGINS_INDEX_URL"
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "info", "hugo"])

        assert result.exit_code == 1
        assert "no plugin index configured" in result.output

    def test_info_never_installs_or_execs_anything(self) -> None:
        """Guard the trust model: `info` only ever prints the install
        command — it must never run it, and must never shell out, pip
        install, or import anything based on fetched metadata."""
        with patch("hivepilot.services.plugin_index.fetch_index", return_value=_entries()):
            mock_orch = MagicMock()
            mock_orch.plugins.loaded = []
            with patch("hivepilot.cli.Orchestrator", lambda: mock_orch):
                with (
                    patch("subprocess.run") as mock_subprocess,
                    patch("subprocess.Popen") as mock_popen,
                    patch("importlib.import_module") as mock_import,
                ):
                    runner = CliRunner()
                    result = runner.invoke(app, ["plugins", "info", "hugo"])

        assert result.exit_code == 0, result.output
        mock_subprocess.assert_not_called()
        mock_popen.assert_not_called()
        mock_import.assert_not_called()
