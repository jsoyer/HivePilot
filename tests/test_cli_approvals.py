"""Tests for `hivepilot approvals approve` / `hivepilot approvals deny` (cli.py).

These CLI commands now go through the shared `Orchestrator.approve_run` helper
instead of calling `run_approved` directly -- regression coverage for the same
pipeline-checkpoint KeyError bug on the CLI channel (mirrors
tests/test_pipeline_checkpoint.py::TestApproveRunRouting,
tests/test_api_service.py::TestApprovalEndpointRouting,
tests/test_slack_bot.py::TestSlackApprovalRoutingThroughSharedHelper,
tests/test_discord_bot.py::TestDiscordApprovalRoutingThroughSharedHelper, and
tests/test_chatops_service.py's routing test classes).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.orchestrator import Orchestrator, RunResult


class _FakeApprovalOrchestrator:
    """Real `Orchestrator.approve_run` bound to fake `resume_pipeline`/
    `run_approved` -- exercises the ACTUAL routing method through the CLI
    commands, not a re-implementation of it."""

    def __init__(self) -> None:
        self.resume_pipeline_calls: list[dict] = []
        self.run_approved_calls: list[dict] = []

    def resume_pipeline(self, **kwargs):
        self.resume_pipeline_calls.append(kwargs)
        return RunResult("noxys", "noxys", kwargs.get("approve", True))

    def run_approved(self, **kwargs):
        self.run_approved_calls.append(kwargs)
        return RunResult("proj", "task", kwargs.get("approve", True))


_FakeApprovalOrchestrator.approve_run = Orchestrator.approve_run  # type: ignore[attr-defined]


def _pipeline_checkpoint_approval() -> dict:
    return {
        "status": "pending",
        "task": "noxys",  # the pipeline name -- NOT a task -- is what KeyErrors
        "metadata": json.dumps({"kind": "pipeline_checkpoint", "pipeline": "noxys"}),
    }


def _per_task_approval() -> dict:
    return {"status": "pending", "task": "build", "metadata": json.dumps({})}


class TestApprovalsApproveRoutingThroughSharedHelper:
    def test_pipeline_checkpoint_approval_routes_to_resume_pipeline(self) -> None:
        """Live-bug regression on the CLI channel: `hivepilot approvals
        approve <run_id>` on a pipeline checkpoint must route to
        `resume_pipeline`, never `run_approved`, and must not raise."""
        fake_orch = _FakeApprovalOrchestrator()
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.cli.Orchestrator", return_value=fake_orch),
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
        ):
            result = runner.invoke(app, ["approvals", "approve", "7"])

        assert result.exit_code == 0, result.output
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.run_approved_calls == []

    def test_per_task_approval_still_routes_to_run_approved(self) -> None:
        """A plain per-task approval via the CLI must keep routing to
        `run_approved` -- unchanged behavior."""
        fake_orch = _FakeApprovalOrchestrator()
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.cli.Orchestrator", return_value=fake_orch),
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_per_task_approval(),
            ),
        ):
            result = runner.invoke(app, ["approvals", "approve", "8"])

        assert result.exit_code == 0, result.output
        assert "approved" in result.output.lower()
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []

    def test_unknown_run_returns_clean_error_no_traceback(self) -> None:
        """A not-pending/unknown run must exit non-zero with a clean error
        message, never let an unhandled exception/traceback surface."""
        fake_orch = _FakeApprovalOrchestrator()
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.cli.Orchestrator", return_value=fake_orch),
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=None),
        ):
            result = runner.invoke(app, ["approvals", "approve", "999"])

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "not pending approval" in result.output
        assert fake_orch.resume_pipeline_calls == []
        assert fake_orch.run_approved_calls == []


class TestApprovalsDenyRoutingThroughSharedHelper:
    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(self) -> None:
        """`hivepilot approvals deny <run_id>` on a pipeline checkpoint must
        also route to `resume_pipeline` (approve=False), not `run_approved`."""
        fake_orch = _FakeApprovalOrchestrator()
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.cli.Orchestrator", return_value=fake_orch),
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
        ):
            result = runner.invoke(app, ["approvals", "deny", "9", "--reason", "not ready"])

        assert result.exit_code == 0, result.output
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.resume_pipeline_calls[0]["approve"] is False
        assert fake_orch.run_approved_calls == []

    def test_deny_per_task_still_routes_to_run_approved(self) -> None:
        """A plain per-task deny via the CLI must keep routing to
        `run_approved` -- unchanged behavior."""
        fake_orch = _FakeApprovalOrchestrator()
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.cli.Orchestrator", return_value=fake_orch),
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_per_task_approval(),
            ),
        ):
            result = runner.invoke(app, ["approvals", "deny", "10", "--reason", "not ready"])

        assert result.exit_code == 0, result.output
        assert "denied" in result.output.lower()
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []

    def test_unknown_run_returns_clean_error_no_traceback(self) -> None:
        """A not-pending/unknown run on deny must also exit non-zero with a
        clean error message, never an unhandled exception/traceback."""
        fake_orch = _FakeApprovalOrchestrator()
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.cli.Orchestrator", return_value=fake_orch),
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=None),
        ):
            result = runner.invoke(app, ["approvals", "deny", "999"])

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "not pending approval" in result.output
        assert fake_orch.resume_pipeline_calls == []
        assert fake_orch.run_approved_calls == []


def test_no_direct_run_approved_call_in_cli_source() -> None:
    """Static guard: the routing decision must live in ONE place
    (`Orchestrator.approve_run`) -- `cli.py` must never call
    `run_approved`/`resume_pipeline` directly again for the approve/deny
    routing decision."""
    from pathlib import Path

    import hivepilot.cli as cli_module

    source = Path(cli_module.__file__).read_text()
    assert ".run_approved(" not in source
    assert ".resume_pipeline(" not in source
    assert ".approve_run(" in source
