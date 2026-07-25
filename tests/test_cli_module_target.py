"""CLI target parsing (monorepo-modules PRD): `hivepilot run <project>/<module>
<task>` — `_resolve_projects` accepts the `<project>/<module>` syntax and
threads it through to `Orchestrator.run_task` unchanged, letting the
orchestrator's `resolve_project_target` do the real resolution. Mirrors
`test_cli_group_wiring.py`'s patching style.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.models import GroupsFile, ProjectConfig


def _monorepo_project(tmp_path) -> ProjectConfig:
    (tmp_path / "apps" / "detection-core").mkdir(parents=True)
    return ProjectConfig(
        path=tmp_path,
        modules={"detection-core": "apps/detection-core"},
    )


def test_run_accepts_project_slash_module_target(tmp_path) -> None:
    """`hivepilot run noxys/detection-core <task>` reaches
    `Orchestrator.run_task` with the target string intact -- resolution
    happens inside the orchestrator, not the CLI."""
    noxys = _monorepo_project(tmp_path)
    mock_orch = MagicMock()
    mock_orch.run_task.return_value = []

    runner = CliRunner()
    with (
        patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
        patch("hivepilot.cli.Orchestrator", return_value=mock_orch),
        patch("hivepilot.cli.load_groups", return_value=GroupsFile(groups={})),
        patch(
            "hivepilot.cli.load_projects",
            return_value=MagicMock(projects={"noxys": noxys}),
        ),
    ):
        result = runner.invoke(app, ["run", "noxys/detection-core", "ship"])

    assert result.exit_code == 0, result.output
    kwargs = mock_orch.run_task.call_args.kwargs
    assert kwargs["project_names"] == ["noxys/detection-core"]


def test_run_plain_project_target_unchanged() -> None:
    """A plain project name (no module) resolves exactly as before this
    PRD -- byte-identical CLI behaviour."""
    mock_orch = MagicMock()
    mock_orch.run_task.return_value = []

    runner = CliRunner()
    with (
        patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
        patch("hivepilot.cli.Orchestrator", return_value=mock_orch),
        patch("hivepilot.cli.load_groups", return_value=GroupsFile(groups={})),
        patch(
            "hivepilot.cli.load_projects",
            return_value=MagicMock(projects={"solo-project": MagicMock()}),
        ),
    ):
        result = runner.invoke(app, ["run", "solo-project", "ship"])

    assert result.exit_code == 0, result.output
    kwargs = mock_orch.run_task.call_args.kwargs
    assert kwargs["project_names"] == ["solo-project"]


def test_run_unknown_module_target_fails_with_clear_message(tmp_path) -> None:
    """`noxys/no-such-module` -- unknown module -- fails at the CLI layer
    with a clear, actionable message (never a raw KeyError)."""
    noxys = _monorepo_project(tmp_path)
    mock_orch = MagicMock()

    runner = CliRunner()
    with (
        patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
        patch("hivepilot.cli.Orchestrator", return_value=mock_orch),
        patch("hivepilot.cli.load_groups", return_value=GroupsFile(groups={})),
        patch(
            "hivepilot.cli.load_projects",
            return_value=MagicMock(projects={"noxys": noxys}),
        ),
    ):
        result = runner.invoke(app, ["run", "noxys/no-such-module", "ship"])

    assert result.exit_code != 0
    assert "no-such-module" in result.output
    assert "detection-core" in result.output
    mock_orch.run_task.assert_not_called()


def test_run_unknown_project_target_still_fails_clearly() -> None:
    """An entirely unknown project (no matching key, group, or module
    split) still fails with a clear "Unknown project" message."""
    mock_orch = MagicMock()

    runner = CliRunner()
    with (
        patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
        patch("hivepilot.cli.Orchestrator", return_value=mock_orch),
        patch("hivepilot.cli.load_groups", return_value=GroupsFile(groups={})),
        patch("hivepilot.cli.load_projects", return_value=MagicMock(projects={})),
    ):
        result = runner.invoke(app, ["run", "nope", "ship"])

    assert result.exit_code != 0
    assert "Unknown project" in result.output
    mock_orch.run_task.assert_not_called()
