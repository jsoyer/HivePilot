"""
Tests for Phase 26b Approach A — `hivepilot plugins search` / `plugins info`
CLI commands (metadata-only plugin discovery index).

CRITICAL: these commands must NEVER trigger a download/exec of plugin code —
only fetch and display index metadata. Every test that exercises the happy
path also asserts no subprocess/pip/import call was made.
"""

from __future__ import annotations

import json
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


def _index_response(entries_payload: list[dict]) -> MagicMock:
    """Mock at the HTTP layer (not `fetch_index`) so the real parsing
    pipeline — including `plugin_index._parse_entry`'s control-char
    stripping — runs, exercising the full defense chain end to end."""
    resp = MagicMock()
    resp.status_code = 200
    body = json.dumps({"plugins": entries_payload}).encode("utf-8")
    resp.iter_content = MagicMock(return_value=iter([body]))
    resp.close = MagicMock()
    return resp


class TestPluginsIndexRenderingIsSanitized:
    """Adversarial-review follow-up (fix-then-ship, MUST/SHOULD-FIX 2):
    every index field is ATTACKER-CONTROLLED (compromised/MITM'd index
    host). A raw ESC/control byte must never survive to the rendered
    output, and Rich markup embedded in a field must never be interpreted
    as a style tag (which would otherwise restyle/hide table content, or
    crash the command on unbalanced tags) — it must show up as literal
    text instead.
    """

    def test_search_strips_control_chars_and_shows_markup_literally(self) -> None:
        malicious = [
            {
                "name": "evil",
                "description": "clean\x1b[31mRED\x1b[0m and [bold red on red]HIDDEN[/]",
            }
        ]
        with (
            patch(
                "hivepilot.services.plugin_index.requests.get",
                return_value=_index_response(malicious),
            ),
            patch(
                "hivepilot.services.plugin_index.settings.plugins_index_url",
                "https://index.example.com/plugins.json",
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "search"])

        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output
        # the injected style tag must appear literally, never applied
        assert "[bold red on red]HIDDEN[/]" in result.output

    def test_info_strips_control_chars_and_shows_markup_literally(self) -> None:
        malicious = [
            {
                "name": "evil",
                "description": "safe [red]x[/] text",
                "checksum": "sha256:\x1b[2Kabc",
                "homepage": "https://example.com/\x07evil",
            }
        ]
        with (
            patch(
                "hivepilot.services.plugin_index.requests.get",
                return_value=_index_response(malicious),
            ),
            patch(
                "hivepilot.services.plugin_index.settings.plugins_index_url",
                "https://index.example.com/plugins.json",
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["plugins", "info", "evil"])

        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output
        assert "\x07" not in result.output
        # rich markup tag shown literally, not applied as a style
        assert "[red]x[/]" in result.output
        # the control byte is gone but the rest of the checksum survives
        assert "sha256:[2Kabc" in result.output
        assert "https://example.com/evil" in result.output
