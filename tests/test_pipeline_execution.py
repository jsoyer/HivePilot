"""
Tests for Sprint 2.0 — Pipeline execution end-to-end (Claude-first).

Covers:
- run_pipeline persists RUNNING then COMPLETE for a happy-path pipeline
- On a failing stage (no continue_on_failure), final status is TEST_FAILURE
  and later stages don't run
- Per-stage artifact is attempted once per stage; with vault_path=None it's
  a no-op (returns None); with a tmp vault + dry_run=False the file exists
- dry-run default: vault not written when dry_run=True
- _runner_for_stage returns 'claude' for any stage
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

# hivepilot.orchestrator is imported at module level so it is in sys.modules
# before any patch("hivepilot.orchestrator.*") context managers are entered.
# conftest.py has already stubbed langchain/boto3 before this import runs.
import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.models import PipelineConfig, PipelineStage
from hivepilot.pipelines import write_stage_artifact
from hivepilot.services.state_service import RunStatus

if TYPE_CHECKING:
    from hivepilot.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(*stage_names: str) -> PipelineConfig:
    """Build a PipelineConfig with the given stage names (task = stage name)."""
    stages = [PipelineStage(name=n, task=n) for n in stage_names]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(pipeline: PipelineConfig) -> "Orchestrator":  # noqa: F821
    """Return a minimal Orchestrator whose pipelines map contains only the given pipeline.

    The patches set during construction are undone when the `with` exits.
    validate_pipeline must be separately patched at call time because run_pipeline
    invokes it after construction; callers are responsible for patching it in the
    outer `with patch(...)` block when calling run_pipeline.
    """
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        orch = Orchestrator()

    return orch


# ---------------------------------------------------------------------------
# RunStatus transition tests
# ---------------------------------------------------------------------------


class TestRunPipelineStatusTransitions:
    """run_pipeline must drive RunStatus transitions through state_service."""

    def test_happy_path_records_running_then_complete(self) -> None:
        """Happy-path: RUNNING is recorded at start, COMPLETE at end."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a", "stage-b")
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch(
                "hivepilot.orchestrator.state_service.record_run_start", return_value=1
            ) as mock_start,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        # record_run_start called with RUNNING status value
        mock_start.assert_called_once()
        call_args = mock_start.call_args
        status_arg = call_args.kwargs.get("status") or (
            call_args.args[2] if len(call_args.args) >= 3 else None
        )
        assert status_arg == RunStatus.RUNNING.value, (
            f"Expected status='{RunStatus.RUNNING.value}', got: {status_arg}"
        )

        # complete_run called with COMPLETE status value
        mock_complete.assert_called_once()
        complete_call = mock_complete.call_args
        actual_status = complete_call.kwargs.get("status") or (
            complete_call.args[1] if len(complete_call.args) >= 2 else None
        )
        assert actual_status == RunStatus.COMPLETE.value, (
            f"Expected COMPLETE status, got: {actual_status}"
        )

    def test_failing_stage_records_test_failure(self) -> None:
        """When a stage fails and continue_on_failure is False, final status is TEST_FAILURE."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a", "stage-b")
        orch = _make_orchestrator_with_pipeline(pipeline)

        run_task_calls: list[str] = []

        def _run_task_fail_first(**kwargs: object) -> list[RunResult]:
            task_name = str(kwargs["task_name"])
            run_task_calls.append(task_name)
            if task_name == "stage-a":
                return [RunResult("proj", task_name, False, "boom")]
            return [RunResult("proj", task_name, True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=2),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_run_task_fail_first),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        # stage-b must NOT have been called (fail-fast)
        assert "stage-b" not in run_task_calls, (
            f"stage-b should not run after stage-a failure; calls were: {run_task_calls}"
        )

        # complete_run must be called with TEST_FAILURE
        mock_complete.assert_called_once()
        complete_call = mock_complete.call_args
        actual_status = complete_call.kwargs.get("status") or (
            complete_call.args[1] if len(complete_call.args) >= 2 else None
        )
        assert actual_status == RunStatus.TEST_FAILURE.value, (
            f"Expected TEST_FAILURE status, got: {actual_status}"
        )


# ---------------------------------------------------------------------------
# Per-stage artifact tests
# ---------------------------------------------------------------------------


class TestPerStageArtifact:
    """write_stage_artifact is called once per stage; vault=None is a no-op."""

    def test_artifact_called_once_per_stage(self) -> None:
        """Artifact write is attempted exactly once for each pipeline stage."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a", "stage-b", "stage-c")
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=3),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch(
                "hivepilot.orchestrator.write_stage_artifact", return_value=None
            ) as mock_artifact,
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        # Exactly 3 calls — one per stage
        assert mock_artifact.call_count == 3, (
            f"Expected 3 artifact calls, got {mock_artifact.call_count}"
        )

    def test_artifact_none_vault_is_noop(self) -> None:
        """write_stage_artifact with vault_path=None returns None (no-op)."""
        result = write_stage_artifact(
            vault_path=None,
            run_id=1,
            stage_name="CEO Intake",
            output="some output",
            dry_run=True,
        )
        assert result is None

    def test_artifact_real_write_creates_file(self, tmp_path: Path) -> None:
        """With dry_run=False and a real vault, the artifact file is created."""
        runs_dir = tmp_path / "12 - HivePilot" / "Runs"
        runs_dir.mkdir(parents=True)

        result = write_stage_artifact(
            vault_path=tmp_path,
            run_id=42,
            stage_name="Implementation",
            output="impl output body",
            dry_run=False,
        )

        assert result is not None
        written_files = list(runs_dir.glob("*.md"))
        assert len(written_files) == 1, f"Expected 1 file in Runs/, found: {written_files}"
        content = written_files[0].read_text()
        assert "impl output body" in content

    def test_dry_run_true_no_file_written(self, tmp_path: Path) -> None:
        """With dry_run=True, no file is physically written to the vault."""
        runs_dir = tmp_path / "12 - HivePilot" / "Runs"
        runs_dir.mkdir(parents=True)

        result = write_stage_artifact(
            vault_path=tmp_path,
            run_id=7,
            stage_name="Review",
            output="review content",
            dry_run=True,
        )

        assert isinstance(result, dict)
        assert result["dry_run"] is True
        written_files = list(runs_dir.glob("*.md"))
        assert written_files == [], f"dry_run=True must not write files; found: {written_files}"


# ---------------------------------------------------------------------------
# _runner_for_stage tests
# ---------------------------------------------------------------------------


class TestRunnerForStage:
    """_runner_for_stage must return 'claude' by default (Claude-first seam)."""

    def test_runner_for_stage_returns_claude(self) -> None:
        from hivepilot.orchestrator import _runner_for_stage

        stage = PipelineStage(name="CEO Intake", task="company-ceo-intake")
        assert _runner_for_stage(stage) == "claude"

    def test_runner_for_unknown_stage_returns_claude(self) -> None:
        from hivepilot.orchestrator import _runner_for_stage

        stage = PipelineStage(name="some-future-stage", task="some-future-task")
        assert _runner_for_stage(stage) == "claude"


# ---------------------------------------------------------------------------
# CLI dry_run kwarg wiring
# ---------------------------------------------------------------------------


class TestCLIDryRun:
    """run_pipeline in orchestrator accepts and honours dry_run kwarg."""

    def test_run_pipeline_accepts_dry_run_kwarg(self) -> None:
        """run_pipeline must accept dry_run as a keyword argument (additive, defaulted)."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a")
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=10),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch(
                "hivepilot.orchestrator.write_stage_artifact", return_value=None
            ) as mock_artifact,
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
        ):
            # Must not raise TypeError
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=False,
            )

        # write_stage_artifact was called with dry_run=False
        for c in mock_artifact.call_args_list:
            actual_dry = c.kwargs.get("dry_run")
            if actual_dry is None and len(c.args) >= 5:
                actual_dry = c.args[4]
            assert actual_dry is False, f"Expected dry_run=False in artifact call, got: {c}"

    def test_dry_run_defaults_to_true(self) -> None:
        """When dry_run is not passed, it defaults to True."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a")
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=11),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch(
                "hivepilot.orchestrator.write_stage_artifact", return_value=None
            ) as mock_artifact,
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                # dry_run intentionally omitted — should default to True
            )

        for c in mock_artifact.call_args_list:
            actual_dry = c.kwargs.get("dry_run")
            if actual_dry is None and len(c.args) >= 5:
                actual_dry = c.args[4]
            assert actual_dry is True, (
                f"Expected dry_run=True (default), got: {actual_dry} in call {c}"
            )
