"""Inter-agent context hand-off (step B).

Non-Claude CLI runners must also inject the user instructions + the outputs of
previous pipeline agents into the prompt, and run_pipeline must accumulate each
stage's output and pass it to the following stages.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hivepilot.config import settings
from hivepilot.models import (
    PipelineConfig,
    PipelinesFile,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskStep,
)
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.prompt_cli_runner import VibeRunner


def _payload(tmp_path: Path, metadata: dict) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="vibe"),
        metadata=metadata,
        secrets={},
    )


def _runner() -> VibeRunner:
    return VibeRunner(RunnerDefinition(name="vibe", kind="vibe", command="vibe"), settings)


def test_prompt_cli_augments_with_extra_and_prior(tmp_path: Path) -> None:
    payload = _payload(tmp_path, {"extra_prompt": "USER_GOAL", "prior_context": "CEO said X"})
    out = _runner()._augment_prompt(payload, "BASE")
    assert "USER_GOAL" in out
    assert "CEO said X" in out
    assert "BASE" in out


def test_prompt_cli_augment_noop_when_empty(tmp_path: Path) -> None:
    payload = _payload(tmp_path, {})
    assert _runner()._augment_prompt(payload, "BASE") == "BASE"


def _make_orch(pipeline: PipelineConfig):
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


def test_run_pipeline_passes_prior_context_to_later_stages() -> None:
    from hivepilot.orchestrator import RunResult

    pipeline = PipelineConfig(
        description="t",
        stages=[PipelineStage(name="alpha", task="alpha"), PipelineStage(name="beta", task="beta")],
    )
    orch = _make_orch(pipeline)
    seen: list = []

    def fake_run_task(**kw):
        seen.append(kw.get("prior_context"))
        return [RunResult("proj", kw["task_name"], True, f"{kw['task_name']} output")]

    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(orch, "run_task", side_effect=fake_run_task),
    ):
        orch.run_pipeline(
            project_names=["proj"],
            pipeline_name="p",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
        )

    assert seen[0] is None  # first stage has no prior context
    assert seen[1] is not None
    assert "alpha output" in seen[1]  # second stage receives the first stage's output
