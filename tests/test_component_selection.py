"""E3 — agents pick the impacted component subset via a COMPONENTS: line."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
from hivepilot.orchestrator import _parse_components


def test_parse_components_extracts_and_intersects() -> None:
    valid = ["acme-api", "acme-web", "acme-worker"]
    text = "Plan...\nCOMPONENTS: acme-api, acme-worker\nmore"
    assert _parse_components(text, valid) == ["acme-api", "acme-worker"]


def test_parse_components_ignores_unknown_and_dedups() -> None:
    valid = ["acme-api"]
    assert _parse_components("COMPONENTS: acme-api, ghost, acme-api", valid) == ["acme-api"]


def test_parse_components_returns_empty_when_absent() -> None:
    assert _parse_components("no marker here", ["acme-api"]) == []


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


def test_only_components_stage_skipped_after_dynamic_narrowing() -> None:
    """A stage scoped to a component that gets dropped during narrowing is skipped."""
    from hivepilot.orchestrator import RunResult

    pipeline = PipelineConfig(
        description="t",
        stages=[
            PipelineStage(name="plan", task="plan"),
            PipelineStage(name="synth", task="synth"),
            PipelineStage(name="build", task="build", pause_before=True),
            # c2 is dropped by the synth's COMPONENTS line below, so this stage
            # (scoped to c2 only) must be skipped once components narrow to c1/c3.
            PipelineStage(name="c2-only", task="c2-only-task", only_components=["c2"]),
            PipelineStage(name="ship", task="ship"),
        ],
    )
    orch = _orch(pipeline)
    targets: dict[str, list[str]] = {}

    def fake_run_task(**kw):
        targets[kw["task_name"]] = list(kw["project_names"])
        detail = "COMPONENTS: c1, c3" if kw["task_name"] == "synth" else "ok"
        return [RunResult(kw["project_names"][0], kw["task_name"], True, detail)]

    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=2),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(orch, "run_task", side_effect=fake_run_task),
    ):
        results = orch.run_pipeline(
            project_names=["hub"],
            pipeline_name="p",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
            simulate=True,
            hub="hub",
            components=["c1", "c2", "c3"],
        )

    assert "c2-only-task" not in targets, "c2-only stage must be skipped: c2 was narrowed out"
    assert targets["ship"] == ["c1", "c3"]  # run continues normally after the skip
    assert any(r.skipped for r in results)
