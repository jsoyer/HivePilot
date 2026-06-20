"""Plan-checkpoint: pause a pipeline for human approval before a flagged stage.

A stage marked ``pause_before: true`` makes run_pipeline stop *before* executing
it: it records a pipeline-checkpoint approval, notifies, and returns. The run is
resumed (or denied) via resume_pipeline once the human reviews the plan.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
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
    from hivepilot.orchestrator import RunResult

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
    from hivepilot.orchestrator import RunResult

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
