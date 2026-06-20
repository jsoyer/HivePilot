"""E3 — agents pick the impacted component subset via a COMPONENTS: line."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
from hivepilot.orchestrator import _parse_components


def test_parse_components_extracts_and_intersects() -> None:
    valid = ["noxys-api", "noxys-web", "noxys-ml"]
    text = "Plan...\nCOMPONENTS: noxys-api, noxys-ml\nmore"
    assert _parse_components(text, valid) == ["noxys-api", "noxys-ml"]


def test_parse_components_ignores_unknown_and_dedups() -> None:
    valid = ["noxys-api"]
    assert _parse_components("COMPONENTS: noxys-api, ghost, noxys-api", valid) == ["noxys-api"]


def test_parse_components_returns_empty_when_absent() -> None:
    assert _parse_components("no marker here", ["noxys-api"]) == []


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


def test_execution_fans_out_only_to_selected_components() -> None:
    from hivepilot.orchestrator import RunResult

    pipeline = PipelineConfig(
        description="t",
        stages=[
            PipelineStage(name="plan", task="plan"),
            PipelineStage(name="synth", task="synth"),
            PipelineStage(name="build", task="build", pause_before=True),
            PipelineStage(name="ship", task="ship"),
        ],
    )
    orch = _orch(pipeline)
    targets: dict[str, list[str]] = {}

    def fake_run_task(**kw):
        targets[kw["task_name"]] = list(kw["project_names"])
        # the synthesizer announces which components the change touches
        detail = "COMPONENTS: c1, c3" if kw["task_name"] == "synth" else "ok"
        return [RunResult(kw["project_names"][0], kw["task_name"], True, detail)]

    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(orch, "run_task", side_effect=fake_run_task),
    ):
        orch.run_pipeline(
            project_names=["hub"],
            pipeline_name="p",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
            simulate=True,  # skip the pause so phase 2 runs in the same call
            hub="hub",
            components=["c1", "c2", "c3"],
        )

    assert targets["plan"] == ["hub"]  # planning on the hub
    assert targets["build"] == ["c1", "c3"]  # narrowed to the selected subset (c2 dropped)
    assert targets["ship"] == ["c1", "c3"]
