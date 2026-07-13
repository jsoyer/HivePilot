"""CLI wiring (PRD A1): `run-pipeline` threads the resolved Group into
Orchestrator.run_pipeline so a stage's only_tags resolves against the group
instead of fail-closing (ValueError) when run via the CLI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.models import Group, GroupsFile


def _group() -> Group:
    return Group(
        description="acme product",
        hub="acme-api",
        components=["acme-web", "acme-worker"],
        tags={"backend": ["acme-api"], "frontend": ["acme-web"]},
    )


def test_run_pipeline_group_mode_threads_group_into_orchestrator() -> None:
    """Group-mode CLI run threads the resolved Group (with .tags) into
    Orchestrator.run_pipeline so PipelineStage.only_tags can resolve against
    the group instead of raising ValueError."""
    grp = _group()
    mock_orch = MagicMock()
    mock_orch.run_pipeline.return_value = []

    runner = CliRunner()
    with (
        patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
        patch("hivepilot.cli.Orchestrator", return_value=mock_orch),
        patch("hivepilot.cli.load_groups", return_value=GroupsFile(groups={"acme": grp})),
    ):
        result = runner.invoke(app, ["run-pipeline", "acme", "ship"])

    assert result.exit_code == 0, result.output
    kwargs = mock_orch.run_pipeline.call_args.kwargs
    assert kwargs["group"] is grp
    assert kwargs["group"].tags == {"backend": ["acme-api"], "frontend": ["acme-web"]}
    assert kwargs["hub"] == "acme-api"
    assert kwargs["components"] == ["acme-web", "acme-worker"]


def test_run_pipeline_non_group_mode_passes_no_group() -> None:
    """The non-group branch must NOT thread a group into run_pipeline."""
    mock_orch = MagicMock()
    mock_orch.run_pipeline.return_value = []

    runner = CliRunner()
    with (
        patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
        patch("hivepilot.cli.Orchestrator", return_value=mock_orch),
        patch("hivepilot.cli.load_groups", return_value=GroupsFile(groups={})),
        patch("hivepilot.cli.resolve_targets", return_value=["solo-project"]),
        patch(
            "hivepilot.cli.load_projects",
            return_value=MagicMock(projects={"solo-project": MagicMock()}),
        ),
    ):
        result = runner.invoke(app, ["run-pipeline", "solo-project", "ship"])

    assert result.exit_code == 0, result.output
    kwargs = mock_orch.run_pipeline.call_args.kwargs
    assert kwargs.get("group") is None
