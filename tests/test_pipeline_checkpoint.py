"""Plan-checkpoint: pause a pipeline for human approval before a flagged stage.

A stage marked ``pause_before: true`` makes run_pipeline stop *before* executing
it: it records a pipeline-checkpoint approval, notifies, and returns. The run is
resumed (or denied) via resume_pipeline once the human reviews the plan.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
from hivepilot.orchestrator import RunResult
from hivepilot.services.state_service import RunStatus


def _pipeline() -> PipelineConfig:
    return PipelineConfig(
        description="test",
        stages=[
            PipelineStage(name="plan", task="plan"),
            PipelineStage(name="build", task="build", pause_before=True),
            PipelineStage(name="ship", task="ship"),
        ],
    )


def _orch(pipeline: PipelineConfig):
    from hivepilot.orchestrator import Orchestrator

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(
            "hivepilot.orchestrator.load_pipelines",
            return_value=PipelinesFile(pipelines={"p": pipeline}),
        ),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        return Orchestrator()


def test_pause_before_stops_pipeline_and_records_checkpoint() -> None:

    orch = _orch(_pipeline())
    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=7),
        patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
        patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval,
        patch("hivepilot.orchestrator.notification_service.send_approval_keyboard"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(
            orch, "run_task", side_effect=lambda **kw: [RunResult("proj", kw["task_name"], True)]
        ) as mock_run_task,
    ):
        results = orch.run_pipeline(
            project_names=["proj"],
            pipeline_name="p",
            extra_prompt="do X",
            auto_git=False,
            dry_run=True,
        )

    # only the planning stage ran; build/ship were NOT executed
    assert mock_run_task.call_count == 1
    assert [r.target for r in results] == ["p:plan"]

    # a pipeline-checkpoint approval was recorded with the resume point
    mock_approval.assert_called_once()
    meta = mock_approval.call_args.args[3]
    assert meta["kind"] == "pipeline_checkpoint"
    assert meta["resume_from_index"] == 1
    assert meta["pipeline"] == "p"
    assert meta["projects"] == ["proj"]

    # run parked as PAUSED (not COMPLETE)
    mock_complete.assert_called_once()
    status = mock_complete.call_args.args[1]
    assert status == RunStatus.PAUSED.value


def test_resume_pipeline_approve_runs_remaining_stages() -> None:

    orch = _orch(_pipeline())
    approval = {
        "status": "pending",
        "metadata": json.dumps(
            {
                "kind": "pipeline_checkpoint",
                "pipeline": "p",
                "projects": ["proj"],
                "resume_from_index": 1,
                "extra_prompt": "do X",
                "auto_git": False,
                "dry_run": True,
                "simulate": False,
            }
        ),
    }
    with (
        patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
        patch("hivepilot.orchestrator.state_service.update_approval") as mock_update,
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=7),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.notification_service.send_notification"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(
            orch, "run_task", side_effect=lambda **kw: [RunResult("proj", kw["task_name"], True)]
        ) as mock_run_task,
    ):
        orch.resume_pipeline(run_id=7, approve=True, approver="me")

    mock_update.assert_called_once()
    assert mock_update.call_args.args[1] == "approved"
    # resumed from index 1 → build + ship ran (plan skipped)
    ran = [c.kwargs["task_name"] for c in mock_run_task.call_args_list]
    assert ran == ["build", "ship"]


def test_resume_pipeline_deny_stops_and_marks_denied() -> None:
    orch = _orch(_pipeline())
    approval = {
        "status": "pending",
        "metadata": json.dumps(
            {
                "kind": "pipeline_checkpoint",
                "pipeline": "p",
                "projects": ["proj"],
                "resume_from_index": 1,
            }
        ),
    }
    with (
        patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
        patch("hivepilot.orchestrator.state_service.update_approval") as mock_update,
        patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
        patch("hivepilot.orchestrator.notification_service.send_notification"),
        patch.object(orch, "run_task") as mock_run_task,
    ):
        result = orch.resume_pipeline(run_id=7, approve=False, approver="me")

    assert result.success is False
    mock_update.assert_called_once()
    assert mock_update.call_args.args[1] == "denied"
    mock_complete.assert_called_once()
    mock_run_task.assert_not_called()


def test_resume_pipeline_auto_git_override() -> None:
    orch = _orch(_pipeline())
    approval = {
        "status": "pending",
        "metadata": json.dumps(
            {
                "kind": "pipeline_checkpoint",
                "pipeline": "p",
                "projects": ["proj"],
                "resume_from_index": 1,
                "auto_git": False,  # launched WITHOUT --auto-git
            }
        ),
    }
    captured: dict = {}

    def fake_run_pipeline(**kw):
        captured.update(kw)
        return []

    with (
        patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
        patch("hivepilot.orchestrator.state_service.update_approval"),
        patch("hivepilot.orchestrator.notification_service.send_notification"),
        patch.object(orch, "run_pipeline", side_effect=fake_run_pipeline),
    ):
        orch.resume_pipeline(run_id=7, approve=True, approver="me", auto_git=True)

    assert captured["auto_git"] is True  # override wins over stored auto_git=False


# ---------------------------------------------------------------------------
# `Orchestrator.approve_run` -- the single shared routing entrypoint used by
# both `api_service.handle_approval` (Pollen's "Approve" button) and
# `telegram_bot._dispatch_approval`. Regression coverage for the live bug:
# the API called `run_approved` unconditionally, and `run_approved` does
# `self.tasks.tasks[task_name]` -- for a pipeline checkpoint `task_name` is
# actually the PIPELINE name (e.g. "noxys"), so it raised a bare `KeyError`
# instead of routing to `resume_pipeline`.
# ---------------------------------------------------------------------------


class TestApproveRunRouting:
    def test_pipeline_checkpoint_routes_to_resume_pipeline_not_run_approved(self) -> None:
        """The live-bug regression: a pipeline-checkpoint approval must call
        `resume_pipeline`, never `run_approved` (which would KeyError on the
        pipeline name, e.g. 'noxys', not being a task)."""
        orch = _orch(_pipeline())
        approval = {
            "status": "pending",
            "task": "noxys",  # the pipeline name, NOT a task -- this is what KeyErrors
            "metadata": json.dumps({"kind": "pipeline_checkpoint", "pipeline": "noxys"}),
        }
        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch.object(
                orch, "resume_pipeline", return_value=RunResult("noxys", "noxys", True)
            ) as mock_resume,
            patch.object(orch, "run_approved") as mock_run_approved,
        ):
            result = orch.approve_run(run_id=7, approve=True, approver="mirador")

        mock_resume.assert_called_once_with(run_id=7, approve=True, approver="mirador")
        mock_run_approved.assert_not_called()
        assert result.success is True

    def test_per_task_approval_routes_to_run_approved_unchanged(self) -> None:
        """A plain per-task approval (no `pipeline_checkpoint` kind) must keep
        going through `run_approved` -- unchanged behavior."""
        orch = _orch(_pipeline())
        approval = {
            "status": "pending",
            "task": "build",
            "metadata": json.dumps({}),
        }
        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch.object(orch, "resume_pipeline") as mock_resume,
            patch.object(
                orch, "run_approved", return_value=RunResult("proj", "build", True)
            ) as mock_run_approved,
        ):
            result = orch.approve_run(
                run_id=8, approve=True, approver="mirador", reason="looks good"
            )

        mock_run_approved.assert_called_once_with(
            run_id=8, approve=True, approver="mirador", reason="looks good"
        )
        mock_resume.assert_not_called()
        assert result.success is True

    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(self) -> None:
        """Deny on a pipeline checkpoint must also route to `resume_pipeline`
        (with approve=False), not `run_approved`."""
        orch = _orch(_pipeline())
        approval = {
            "status": "pending",
            "task": "noxys",
            "metadata": json.dumps({"kind": "pipeline_checkpoint", "pipeline": "noxys"}),
        }
        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch.object(
                orch, "resume_pipeline", return_value=RunResult("noxys", "noxys", False)
            ) as mock_resume,
            patch.object(orch, "run_approved") as mock_run_approved,
        ):
            result = orch.approve_run(run_id=9, approve=False, approver="mirador")

        mock_resume.assert_called_once_with(run_id=9, approve=False, approver="mirador")
        mock_run_approved.assert_not_called()
        assert result.success is False

    def test_unknown_run_raises_value_error_not_key_error(self) -> None:
        """No approval row at all -- must raise `ValueError` (caller maps it
        to a 4xx), never a bare `KeyError`/`AttributeError`."""
        orch = _orch(_pipeline())
        with patch("hivepilot.orchestrator.state_service.get_approval", return_value=None):
            with pytest.raises(ValueError, match="not pending approval"):
                orch.approve_run(run_id=999, approve=True, approver="mirador")

    def test_already_resolved_run_raises_value_error(self) -> None:
        """A run whose approval is already approved/denied is not
        actionable again -- `ValueError`, not a crash."""
        orch = _orch(_pipeline())
        approval = {"status": "approved", "task": "build", "metadata": "{}"}
        with patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval):
            with pytest.raises(ValueError, match="not pending approval"):
                orch.approve_run(run_id=10, approve=True, approver="mirador")
