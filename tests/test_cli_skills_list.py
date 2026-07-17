"""Tests for Sprint 5 (skill-plugin-type PRD) — `hivepilot skills list` CLI
command.

Covers:
- `skills list` exits 0 and renders a registered skill's name/description/
  provider/applies_to (mocked `Orchestrator`, mirroring
  tests/test_cli_plugins_list.py's pattern).
- `skills list` shows a placeholder when no skill is registered, and shows
  "any" when a skill declares no `applies_to`.

Tests for `plugins/sample_skill.py` itself (register() shape + real
PluginManager discovery + plugins_disabled gating) live in
tests/test_sample_skill.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from hivepilot.cli import app


class TestSkillsListCommand:
    def test_exits_zero_and_renders_registered_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.list_skills.return_value = [
            {
                "name": "sample-skill",
                "description": "Trivial example skill demonstrating the SkillSpec contract.",
                "provider": "sample_skill",
                "files": {"SKILL.md": "# Sample Skill\n"},
                "applies_to": ["claude"],
            }
        ]
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["skills", "list"])

        assert result.exit_code == 0, result.output
        assert "sample-skill" in result.output
        assert "Trivial example skill demonstrating the SkillSpec contract." in result.output
        assert "sample_skill" in result.output
        assert "claude" in result.output

    def test_applies_to_defaults_to_any_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.list_skills.return_value = [
            {"name": "s1", "description": "d", "provider": "p", "files": {}},
        ]
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["skills", "list"])

        assert result.exit_code == 0, result.output
        assert "any" in result.output.lower()

    def test_placeholder_when_no_skills_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.list_skills.return_value = []
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["skills", "list"])

        assert result.exit_code == 0, result.output
        assert "Skills" in result.output
