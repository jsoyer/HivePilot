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

import pytest

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


def _make_pipeline_with_stages(*stages: PipelineStage) -> PipelineConfig:
    """Build a PipelineConfig from pre-built PipelineStage instances (so callers
    can set only_components/only_tags/continue_on_failure directly)."""
    return PipelineConfig(description="test pipeline", stages=list(stages))


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

        stage = PipelineStage(name="CEO Intake", task="ceo-intake")
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


# ---------------------------------------------------------------------------
# PRD A1 / Sprint 1 — stage scoping (only_components / only_tags) +
# continue_on_failure
# ---------------------------------------------------------------------------


class TestStageScoping:
    """only_components / only_tags gate whether a stage's task is invoked.

    Skip semantics (frozen contract): target = union of only_components and the
    components reachable via only_tags (through Group.tags). A stage is skipped
    iff target is non-empty AND disjoint from the selected components. A
    skipped stage never invokes its task, is not counted as a failure, and
    leaves prior_chunks untouched.
    """

    @staticmethod
    def _run(
        orch: "Orchestrator",  # noqa: F821
        *,
        run_task_side_effect,
        components: list[str],
        group_tags: dict[str, list[str]] | None = None,
        run_id: int = 200,
    ) -> None:
        with (
            patch(
                "hivepilot.orchestrator.state_service.record_run_start", return_value=run_id
            ),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=run_task_side_effect),
        ):
            orch.run_pipeline(
                project_names=["hub"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
                simulate=True,
                hub="hub",
                components=components,
                group_tags=group_tags,
            )

    def test_skip_excludes_stage_when_target_disjoint(self) -> None:
        """only_components disjoint from the selected components -> stage skipped."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="only-b", task="only-b", only_components=["comp-b"]),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        self._run(orch, run_task_side_effect=_run_task, components=["comp-a"])

        assert calls == [], f"skipped stage's task must not be invoked, got calls={calls}"

    def test_no_skip_when_only_components_matches(self) -> None:
        """only_components intersects the selected components -> stage runs."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="only-a", task="only-a", only_components=["comp-a"]),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        self._run(orch, run_task_side_effect=_run_task, components=["comp-a"])

        assert calls == ["only-a"]

    def test_no_selector_always_runs(self) -> None:
        """A stage with neither only_components nor only_tags always runs."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="plain", task="plain"),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        self._run(orch, run_task_side_effect=_run_task, components=["comp-zzz"])

        assert calls == ["plain"]

    def test_only_tags_match_runs_stage(self) -> None:
        """only_tags resolves through Group.tags; matching selected component runs."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="backend-only", task="backend-only", only_tags=["backend"]),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        self._run(
            orch,
            run_task_side_effect=_run_task,
            components=["comp-a"],
            group_tags={"backend": ["comp-a", "comp-b"]},
        )

        assert calls == ["backend-only"]

    def test_union_of_only_components_and_only_tags(self) -> None:
        """target = only_components UNION components-from-only_tags; either can match."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(
                name="union-stage",
                task="union-stage",
                only_components=["comp-x"],
                only_tags=["grp1"],
            ),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        # comp-y is only reachable via the only_tags branch (not only_components),
        # proving the union (not intersection) is being evaluated.
        self._run(
            orch,
            run_task_side_effect=_run_task,
            components=["comp-y"],
            group_tags={"grp1": ["comp-y"]},
        )

        assert calls == ["union-stage"]

    def test_undefined_tag_raises(self) -> None:
        """An only_tags value absent from the run's Group.tags fails closed."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="ghost-tag", task="ghost-tag", only_tags=["ghost"]),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)

        def _run_task(**kw):
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        with pytest.raises(ValueError, match="ghost"):
            self._run(
                orch,
                run_task_side_effect=_run_task,
                components=["comp-a"],
                group_tags={"backend": ["comp-a"]},
            )

    def test_continue_on_failure_true_suppresses_break(self) -> None:
        """continue_on_failure=True on a failing stage must not fail-fast the run."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="stage-a", task="stage-a", continue_on_failure=True),
            PipelineStage(name="stage-b", task="stage-b"),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            if kw["task_name"] == "stage-a":
                return [RunResult(kw["project_names"][0], kw["task_name"], False, "boom")]
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=201),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_run_task),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        assert calls == ["stage-a", "stage-b"], (
            f"stage-b must still run when stage-a has continue_on_failure=True: {calls}"
        )
        actual_status = mock_complete.call_args.kwargs.get("status") or (
            mock_complete.call_args.args[1] if len(mock_complete.call_args.args) >= 2 else None
        )
        assert actual_status == RunStatus.COMPLETE.value, (
            f"continue_on_failure=True must not flip final status to TEST_FAILURE, "
            f"got: {actual_status}"
        )

    def test_continue_on_failure_absent_preserves_fail_fast(self) -> None:
        """continue_on_failure defaulted (False/absent) must still fail-fast."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="stage-a", task="stage-a"),
            PipelineStage(name="stage-b", task="stage-b"),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _run_task(**kw):
            calls.append(kw["task_name"])
            if kw["task_name"] == "stage-a":
                return [RunResult(kw["project_names"][0], kw["task_name"], False, "boom")]
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=202),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_run_task),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        assert calls == ["stage-a"], f"stage-b must not run after fail-fast: {calls}"
        actual_status = mock_complete.call_args.kwargs.get("status") or (
            mock_complete.call_args.args[1] if len(mock_complete.call_args.args) >= 2 else None
        )
        assert actual_status == RunStatus.TEST_FAILURE.value

    def test_skipped_stage_not_in_prior_chunks(self) -> None:
        """A skipped stage's output must not leak into prior_chunks for later stages."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="skip-me", task="skip-me", only_components=["comp-b"]),
            PipelineStage(name="run-me", task="run-me"),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        captured_prior_context: dict[str, str | None] = {}

        def _run_task(**kw):
            if kw["task_name"] == "run-me":
                captured_prior_context["run-me"] = kw.get("prior_context")
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        self._run(orch, run_task_side_effect=_run_task, components=["comp-a"])

        prior_context = captured_prior_context.get("run-me") or ""
        assert "skip-me" not in prior_context, (
            f"skipped stage must not appear in downstream prior_chunks: {prior_context!r}"
        )

    def test_skipped_stage_result_flag(self) -> None:
        """A skipped stage is represented at stage level via RunResult.skipped=True,
        and its success flag never counts as a failure for downstream fail-fast."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_with_stages(
            PipelineStage(name="skip-me", task="skip-me", only_components=["comp-b"]),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)

        def _run_task(**kw):
            return [RunResult(kw["project_names"][0], kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=203),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_run_task),
        ):
            results = orch.run_pipeline(
                project_names=["hub"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
                simulate=True,
                hub="hub",
                components=["comp-a"],
            )

        assert len(results) == 1
        assert results[0].skipped is True
        assert results[0].success is True  # not counted as a failure
