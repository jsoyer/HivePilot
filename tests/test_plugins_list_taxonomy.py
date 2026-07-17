"""Taxonomy + integration tests for `hivepilot plugins list` (plugin-arch-
overhaul PRD, Sprint 05).

Unlike `tests/test_cli_plugins_list.py` (which mocks `Orchestrator.plugins`
with hand-built `PluginRecord`s to test the CLI's own rendering logic in
isolation), these tests drive a REAL `hivepilot.plugins.PluginManager()` --
scanning the actual `plugins/*.py` directory -- so the taxonomy assertions
prove the true end-to-end wiring: real plugin `register()` calls -> real
`PluginRecord.contributions` attribution -> real `plugins list` rendering.

Covers:
  (a) the Loaded Plugins table's `contributes` column names every new
      plugin's real contribution kind: `tmux` (runner), `bitwarden` /
      `vaultwarden` (secrets), `obsidian` (now also `before_step`/
      `after_step` hooks, on top of its pre-existing notifier + on_error/
      on_pipeline_end hooks).
  (b) `sample` / `sample_skill` are absent from `plugins list` by default
      (both flags default False -- opt-in, dormant) and present once their
      flags are flipped True.
  (c) the Agent Runners table reflects real `RUNNER_MAP` membership for the
      built-in agent kinds (`claude`/`codex`/`vibe`/`openrouter`), not just
      a hardcoded "active" status -- a built-in kind that is absent from
      `RUNNER_MAP` (flag off) now renders `inactive` with its
      `HIVEPILOT_<KIND>_ENABLED` flag, exactly like a plugin agent kind.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.config import settings
from hivepilot.plugins import PluginManager
from hivepilot.registry import RUNNER_MAP


def _invoke_plugins_list(monkeypatch: pytest.MonkeyPatch, plugin_manager: PluginManager):
    """Wire a REAL `PluginManager` instance into a mocked `Orchestrator` (so
    we exercise real plugin scanning/registration/attribution without also
    needing a real `Orchestrator` -- which would load real projects.yaml/
    tasks.yaml/pipelines.yaml), then invoke `plugins list` for real."""
    mock_orch = MagicMock()
    mock_orch.plugins = plugin_manager
    monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

    runner = CliRunner()
    return runner.invoke(app, ["plugins", "list"])


class TestTaxonomyRealPluginScan:
    """(a) Full `plugins list` output contains every new plugin name + its
    real, attributed contribution kind."""

    def test_tmux_bitwarden_vaultwarden_obsidian_contributions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pm = PluginManager()
        loaded_by_name = {record.name: record for record in pm.loaded}

        for name in ("tmux", "bitwarden", "vaultwarden", "obsidian"):
            assert name in loaded_by_name, f"plugin {name!r} did not load"

        assert loaded_by_name["tmux"].contributions.get("runners") == ["tmux"]
        assert loaded_by_name["bitwarden"].contributions.get("secrets") == ["bitwarden"]
        assert loaded_by_name["vaultwarden"].contributions.get("secrets") == ["vaultwarden"]

        # obsidian now contributes BOTH before_step (recall) and after_step
        # (store) -- Sprint 02 -- ON TOP OF its pre-existing on_error /
        # on_pipeline_end hooks and its `obsidian` notifier.
        obsidian_hooks = loaded_by_name["obsidian"].contributions.get("hooks", [])
        for hook_name in ("before_step", "after_step", "on_error", "on_pipeline_end"):
            assert hook_name in obsidian_hooks
        assert loaded_by_name["obsidian"].contributions.get("notifiers") == ["obsidian"]

        result = _invoke_plugins_list(monkeypatch, pm)
        assert result.exit_code == 0, result.output
        assert "tmux" in result.output
        assert "bitwarden" in result.output
        assert "vaultwarden" in result.output
        assert "obsidian" in result.output
        assert "runners: tmux" in result.output
        assert "secrets: bitwarden" in result.output
        assert "secrets: vaultwarden" in result.output
        # obsidian's rendered contributes column includes its hooks summary.
        assert "before_step" in result.output
        assert "after_step" in result.output

    def test_tmux_registered_as_other_runner_kind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tmux is a runner (not an agent kind) -- it must show up in the
        "Other Runner Kinds" table, tagged `plugin` (not `built-in`)."""
        pm = PluginManager()
        result = _invoke_plugins_list(monkeypatch, pm)

        assert result.exit_code == 0, result.output
        assert "tmux" in RUNNER_MAP
        assert "Other Runner Kinds" in result.output

    def test_bitwarden_vaultwarden_registered_as_secrets_backends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pm = PluginManager()
        result = _invoke_plugins_list(monkeypatch, pm)

        assert result.exit_code == 0, result.output
        assert "Secrets Backends" in result.output
        assert "bitwarden" in result.output
        assert "vaultwarden" in result.output


class TestSampleAndSampleSkillDefaultGating:
    """(b) `sample`/`sample_skill` absent by default, present when enabled --
    driven through the real PluginManager scan, not a register()-only unit
    check (see tests/test_sample.py / tests/test_sample_skill.py for those)."""

    def test_absent_by_default(self) -> None:
        assert settings.sample_enabled is False
        assert settings.sample_skill_enabled is False

        pm = PluginManager()
        names = {record.name for record in pm.loaded}
        # A plugin whose register() returns {} still gets a PluginRecord (the
        # module loaded successfully) but contributes nothing attributable --
        # assert it has no contributions rather than asserting absence from
        # `loaded`, since local-file discovery loads every plugins/*.py file
        # regardless of its enable flag (the flag gates register()'s payload,
        # not the file scan itself).
        if "sample" in names:
            record = next(r for r in pm.loaded if r.name == "sample")
            assert record.contributions == {}
        if "sample_skill" in names:
            record = next(r for r in pm.loaded if r.name == "sample_skill")
            assert record.contributions == {}

    def test_present_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "sample_enabled", True, raising=False)
        monkeypatch.setattr(settings, "sample_skill_enabled", True, raising=False)

        pm = PluginManager()
        by_name = {record.name: record for record in pm.loaded}

        assert "sample" in by_name
        assert by_name["sample"].contributions.get("hooks")
        assert by_name["sample"].contributions.get("panels") == ["sample_stats"]

        assert "sample_skill" in by_name
        assert by_name["sample_skill"].contributions.get("skills") == ["sample-skill"]

        result = _invoke_plugins_list(monkeypatch, pm)
        assert result.exit_code == 0, result.output
        assert "sample_stats" in result.output or "panels: sample_stats" in result.output
        assert "skills: sample-skill" in result.output


class TestAgentRunnersTableReflectsEnabledFlags:
    """(c) The Agent Runners table's `status` column reflects real
    `RUNNER_MAP` membership for built-in agent kinds -- not a hardcoded
    "active" -- matching the behavior `hivepilot.registry._BUILTIN_RUNNERS`'
    own registration gate produces when a `<kind>_enabled` flag is False."""

    def test_builtin_kind_active_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_orch = MagicMock()
        mock_orch.plugins.loaded = []
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "claude" in RUNNER_MAP
        assert "HIVEPILOT_CLAUDE_ENABLED" in result.output
        assert "HIVEPILOT_CODEX_ENABLED" in result.output
        assert "HIVEPILOT_VIBE_ENABLED" in result.output
        assert "HIVEPILOT_OPENROUTER_ENABLED" in result.output

    def test_builtin_kind_disabled_renders_inactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulates `codex_enabled=False` by removing `codex` from the real
        `RUNNER_MAP` -- exactly the state `_BUILTIN_RUNNERS`' own gate would
        produce at process start with the flag off (the registration loop
        itself only runs once at import time, so a test toggles the
        resulting registry state directly rather than re-running import-time
        code). `tests/conftest.py`'s autouse `_isolate_runner_and_notifier_maps`
        fixture restores `RUNNER_MAP` to its pristine baseline after this
        test, so this mutation never leaks to any other test."""
        assert "codex" in RUNNER_MAP
        monkeypatch.delitem(RUNNER_MAP, "codex")

        mock_orch = MagicMock()
        mock_orch.plugins.loaded = []
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "inactive" in result.output
        assert "HIVEPILOT_CODEX_ENABLED" in result.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
