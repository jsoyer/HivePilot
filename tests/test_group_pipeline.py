"""E2 — group-scoped pipeline: planning runs in the hub, execution fans out.

Planning stages (before the pause_before checkpoint) run once on the hub with the
component manifest in context; execution stages run on every component.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage


def _pipeline() -> PipelineConfig:
    return PipelineConfig(
        description="t",
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


def test_group_mode_planning_on_hub_execution_on_components() -> None:
    from hivepilot.orchestrator import RunResult

    orch = _orch(_pipeline())
    targets: dict[str, list[str]] = {}
    contexts: dict[str, str | None] = {}

    def fake_run_task(**kw):
        targets[kw["task_name"]] = list(kw["project_names"])
        contexts[kw["task_name"]] = kw.get("prior_context")
        return [RunResult(kw["project_names"][0], kw["task_name"], True)]

    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(orch, "run_task", side_effect=fake_run_task),
    ):
        orch.run_pipeline(
            project_names=["hubrepo"],
            pipeline_name="p",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
            simulate=True,  # skip the pause so all stages run in one call
            hub="hubrepo",
            components=["c1", "c2"],
        )

    assert targets["plan"] == ["hubrepo"]  # planning runs on the hub
    assert targets["build"] == ["c1", "c2"]  # execution fans out
    assert targets["ship"] == ["c1", "c2"]
    assert "c1" in (contexts["plan"] or "")  # component manifest fed to planning


def test_group_checkpoint_stores_hub_and_components() -> None:
    from hivepilot.orchestrator import RunResult

    orch = _orch(_pipeline())
    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=9),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_appr,
        patch("hivepilot.orchestrator.notification_service.send_approval_keyboard"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(
            orch, "run_task", side_effect=lambda **kw: [RunResult("hubrepo", kw["task_name"], True)]
        ),
    ):
        orch.run_pipeline(
            project_names=["hubrepo"],
            pipeline_name="p",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
            hub="hubrepo",
            components=["c1", "c2"],
        )

    meta = mock_appr.call_args.args[3]
    assert meta["hub"] == "hubrepo"
    assert meta["components"] == ["c1", "c2"]
    assert "c1" in (meta["planning_context"] or "")  # planning context carried for resume
