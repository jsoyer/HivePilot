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
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

# hivepilot.orchestrator is imported at module level so it is in sys.modules
# before any patch("hivepilot.orchestrator.*") context managers are entered.
# conftest.py has already stubbed langchain/boto3 before this import runs.
import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.models import PipelineConfig, PipelineStage, TaskConfig
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
# PRD A1 — stage scoping (only_components / only_tags) + continue_on_failure
# ---------------------------------------------------------------------------


class TestStageScoping:
    """run_pipeline skips a stage whose scoping target is disjoint from the
    run's selected components; a stage with neither selector always runs."""

    def test_skip_excludes_stage_when_disjoint(self) -> None:
        """A stage scoped to a component not in this run is skipped: its task
        is never invoked, and it is recorded in `results` as skipped=True —
        distinguishable from both a success and a failure (PRD A1 §6)."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[
                PipelineStage(name="build", task="build", only_components=["c9"]),
                PipelineStage(name="ship", task="ship"),
            ],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def fake_run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult("proj", kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=100),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch(
                "hivepilot.orchestrator.write_stage_artifact", return_value=None
            ) as mock_artifact,
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=fake_run_task),
        ):
            results = orch.run_pipeline(
                project_names=["c1"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
                simulate=True,
                components=["c1", "c2"],
            )

        assert calls == ["ship"], f"'build' must be skipped (task never invoked), got: {calls}"

        by_target = {r.target: r for r in results}
        assert set(by_target) == {"test-pipe:build", "test-pipe:ship"}

        build_result = by_target["test-pipe:build"]
        assert build_result.skipped is True
        assert build_result.success is True, "a skip must never be recorded as a failure"

        ship_result = by_target["test-pipe:ship"]
        assert ship_result.skipped is False
        assert ship_result.success is True

        # skipped stage's output must never reach the vault artifact / prior_chunks
        artifact_stages = [c.kwargs.get("stage_name") for c in mock_artifact.call_args_list]
        assert artifact_stages == ["ship"], (
            f"skipped stage's output must not reach prior_chunks/artifact, got: {artifact_stages}"
        )

    def test_skipped_stage_not_in_prior_chunks(self) -> None:
        """prior_chunks is untouched by a skipped stage: the next stage's
        prior_context (built from prior_chunks) carries no trace of it."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[
                PipelineStage(name="build", task="build", only_components=["c9"]),
                PipelineStage(name="ship", task="ship"),
            ],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        prior_contexts: dict[str, object] = {}

        def fake_run_task(**kw):
            prior_contexts[kw["task_name"]] = kw.get("prior_context")
            return [RunResult("proj", kw["task_name"], True, "some output")]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=106),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=fake_run_task),
        ):
            # Not in group mode (no `components`), so selected_components stays
            # [] — irrelevant here: the point is 'build' (only_components=["c9"])
            # is skipped regardless, and must leave no trace in prior_chunks.
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        # 'build' was skipped and never invoked, so it never appended anything
        # to prior_chunks — 'ship' (the only stage that ran) sees no prior
        # context at all.
        assert list(prior_contexts) == ["ship"]
        assert prior_contexts["ship"] is None

    def test_no_skip_when_only_components_matches_selected(self) -> None:
        """A stage scoped to a component that IS in this run's selection runs."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[
                PipelineStage(name="build", task="build", only_components=["c1"]),
            ],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _record(**kw: Any) -> list[RunResult]:
            calls.append(kw["task_name"])
            return [RunResult("proj", kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=101),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_record),
        ):
            orch.run_pipeline(
                project_names=["c1"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
                simulate=True,
                components=["c1", "c2"],
            )

        assert calls == ["build"]

    def test_no_selector_stage_always_runs(self) -> None:
        """A stage with neither only_components nor only_tags always runs,
        even in group mode with a narrow component selection."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[PipelineStage(name="plain", task="plain")],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def _record(**kw: Any) -> list[RunResult]:
            calls.append(kw["task_name"])
            return [RunResult("proj", kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=102),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_record),
        ):
            orch.run_pipeline(
                project_names=["c1"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
                simulate=True,
                components=["c1"],
            )

        assert calls == ["plain"]

    def test_only_tags_match_runs_via_group(self) -> None:
        """only_tags resolves against the run's Group.tags mapping; a stage
        whose tag-resolved components overlap the selection runs."""
        from hivepilot.models import Group
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[PipelineStage(name="frontend-review", task="review", only_tags=["frontend"])],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []
        group = Group(
            description="d", hub="hub", components=["web", "api"], tags={"frontend": ["web"]}
        )

        def _record(**kw: Any) -> list[RunResult]:
            calls.append(kw["task_name"])
            return [RunResult("proj", kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=103),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=_record),
        ):
            orch.run_pipeline(
                project_names=["web"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
                simulate=True,
                components=["web"],
                group=group,
            )

        assert calls == ["review"]

    def test_undefined_tag_raises_value_error(self) -> None:
        """Fail-closed: run_pipeline must raise ValueError up front (before any
        stage runs) when a stage's only_tags references a tag not present in
        the run's Group.tags — regardless of whether a group was even passed."""
        import pytest

        pipeline = PipelineConfig(
            description="t",
            stages=[PipelineStage(name="security-review", task="review", only_tags=["security"])],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start") as mock_start,
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        ):
            with pytest.raises(ValueError, match="security"):
                orch.run_pipeline(
                    project_names=["proj"],
                    pipeline_name="test-pipe",
                    extra_prompt=None,
                    auto_git=False,
                    dry_run=True,
                )
            mock_start.assert_not_called()  # fails before any run/stage bookkeeping starts


class TestContinueOnFailure:
    """continue_on_failure controls whether a failed stage fail-fasts the run."""

    def test_continue_on_failure_true_suppresses_fail_fast(self) -> None:
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[
                PipelineStage(name="stage-a", task="stage-a", continue_on_failure=True),
                PipelineStage(name="stage-b", task="stage-b"),
            ],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def fake_run_task(**kw):
            calls.append(kw["task_name"])
            if kw["task_name"] == "stage-a":
                return [RunResult("proj", kw["task_name"], False, "boom")]
            return [RunResult("proj", kw["task_name"], True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=104),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=fake_run_task),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        assert calls == ["stage-a", "stage-b"], (
            f"stage-b must still run — continue_on_failure=True suppresses fail-fast: {calls}"
        )

    def test_continue_on_failure_false_preserves_fail_fast(self) -> None:
        """Explicit continue_on_failure=False on the failing stage still breaks
        the run before later stages (same as the absent/default case)."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="t",
            stages=[
                PipelineStage(name="stage-a", task="stage-a", continue_on_failure=False),
                PipelineStage(name="stage-b", task="stage-b"),
            ],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        calls: list[str] = []

        def fake_run_task(**kw):
            calls.append(kw["task_name"])
            return [RunResult("proj", kw["task_name"], False, "boom")]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=105),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task", side_effect=fake_run_task),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        assert calls == ["stage-a"], f"stage-b must NOT run (fail-fast): {calls}"
        actual_status = (
            mock_complete.call_args.kwargs.get("status") or (mock_complete.call_args.args[1])
        )
        assert actual_status == RunStatus.TEST_FAILURE.value


# ---------------------------------------------------------------------------
# PRD A2 Sprint 1 — keyed store (_parse_output_sections / _stage_outputs_by_key)
# ---------------------------------------------------------------------------


class TestParseOutputSections:
    """`_parse_output_sections` extracts `## <HEADER>` sections keyed by the
    normalized key they match; returns {} when nothing matches (mirrors
    `_parse_components`'s empty-when-none-found style)."""

    def test_extracts_matching_section_to_key(self) -> None:
        from hivepilot.orchestrator import _parse_output_sections

        text = "intro line\n## DESIGN_SPEC\nline one\nline two\n"
        result = _parse_output_sections(text, ["design_spec"])
        assert result == {"design_spec": "line one\nline two"}

    def test_section_stops_at_next_header(self) -> None:
        from hivepilot.orchestrator import _parse_output_sections

        text = "## DESIGN_SPEC\nbody a\n## UI_REVIEW\nbody b\n"
        result = _parse_output_sections(text, ["design_spec", "ui_review"])
        assert result == {"design_spec": "body a", "ui_review": "body b"}

    def test_key_with_no_matching_header_is_absent(self) -> None:
        from hivepilot.orchestrator import _parse_output_sections

        text = "## DESIGN_SPEC\nbody a\n"
        result = _parse_output_sections(text, ["design_spec", "no_such_key"])
        assert "no_such_key" not in result
        assert result == {"design_spec": "body a"}

    def test_no_headers_returns_empty_dict(self) -> None:
        from hivepilot.orchestrator import _parse_output_sections

        text = "just some plain prose, no headers at all"
        assert _parse_output_sections(text, ["design_spec"]) == {}

    def test_case_and_separator_insensitive_header_match(self) -> None:
        """`design_spec` matches `## DESIGN_SPEC`, `## Design Spec`, and
        `## design-spec` — key normalization treats `_`, `-`, and spaces as
        equivalent, case-insensitively."""
        from hivepilot.orchestrator import _parse_output_sections

        for header in ("## DESIGN_SPEC", "## Design Spec", "## design-spec"):
            text = f"{header}\ncontent here\n"
            result = _parse_output_sections(text, ["design_spec"])
            assert result == {"design_spec": "content here"}, f"failed for header: {header!r}"

    def test_unrelated_subheader_does_not_match(self) -> None:
        """`### ` (3 hashes) is not a `## ` section header — must not be
        mistaken for a match, and must not swallow the enclosing section."""
        from hivepilot.orchestrator import _parse_output_sections

        text = "## DESIGN_SPEC\nbody\n### not a top section\nmore body\n"
        result = _parse_output_sections(text, ["design_spec"])
        assert result == {"design_spec": "body\n### not a top section\nmore body"}


class TestStageOutputsByKey:
    """`_stage_outputs_by_key` maps a role's declared output keys to content:
    section-extracted where present, else the whole stage_output blob
    (coarse fallback) — every declared key always resolves to something."""

    def test_coarse_fallback_stores_whole_blob_for_key_with_no_section(self) -> None:
        from hivepilot.orchestrator import _stage_outputs_by_key

        stage_output = "general prose with no ## headers describing the work done"
        result = _stage_outputs_by_key(stage_output, ["implementation"])
        assert result == {"implementation": stage_output}

    def test_multiple_outputs_no_sections_all_map_to_whole_blob(self) -> None:
        """A role with 2 declared outputs and no matching sections in the
        stage output maps the whole blob to each key."""
        from hivepilot.orchestrator import _stage_outputs_by_key

        stage_output = "plain implementation notes, no section headers"
        result = _stage_outputs_by_key(stage_output, ["implementation", "implementation_notes"])
        assert result == {
            "implementation": stage_output,
            "implementation_notes": stage_output,
        }

    def test_mixed_section_and_fallback_keys(self) -> None:
        """One declared key has a matching section; the other falls back to
        the whole blob."""
        from hivepilot.orchestrator import _stage_outputs_by_key

        stage_output = "## DESIGN_SPEC\nthe spec body\nintro\n## OTHER\nfiller"
        result = _stage_outputs_by_key(stage_output, ["design_spec", "ui_review"])
        assert result == {
            "design_spec": "the spec body\nintro",
            "ui_review": stage_output,
        }


class TestKeyedStoreInertThisSprint:
    """The run-scoped keyed store is populated during run_pipeline but never
    consumed — prior_chunks / build_prior_context stay byte-identical whether
    or not the stage's role declares outputs (backward-compat guarantee)."""

    def test_prior_context_unchanged_when_role_has_outputs(self) -> None:
        """A 2-stage pipeline where stage-a's role declares outputs and emits
        a `## DESIGN_SPEC` section: the store gets populated, but the second
        stage's prior_context is built exactly as before (full stage_output
        blob, untouched by the section extraction)."""
        from hivepilot.models import PipelinesFile
        from hivepilot.orchestrator import Orchestrator, RunResult
        from hivepilot.roles import Role

        pipeline = PipelineConfig(
            description="t",
            stages=[
                PipelineStage(name="stage-a", task="task-a"),
                PipelineStage(name="stage-b", task="task-b"),
            ],
        )
        pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})

        fake_role = Role(
            name="designer",
            title="Designer",
            prompt_file=Path("prompts/agents/designer.md"),
            model_profile="coding",
            inputs=[],
            outputs=["design_spec", "ui_review"],
            can_block=False,
            order=1,
        )
        tasks_obj = MagicMock(
            tasks={
                "task-a": TaskConfig(description="a", role="designer"),
                "task-b": TaskConfig(description="b"),
            },
            runners={},
        )

        design_output = "## DESIGN_SPEC\nspec body\nmore spec body"

        def fake_run_task(**kw):
            if kw["task_name"] == "task-a":
                return [RunResult("proj", "task-a", True, design_output)]
            return [RunResult("proj", "task-b", True, "b output")]

        with (
            patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
            patch("hivepilot.orchestrator.load_tasks", return_value=tasks_obj),
            patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
            patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
            patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        ):
            orch = Orchestrator()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=200),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.roles.ROLES", {"designer": fake_role}),
            patch.object(orch, "run_task", side_effect=fake_run_task) as mock_run_task,
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        # Two calls: stage-a (no prior context) then stage-b (fed stage-a's
        # full, unextracted output via prior_chunks/build_prior_context).
        assert mock_run_task.call_count == 2
        second_call_kwargs = mock_run_task.call_args_list[1].kwargs
        prior_context = second_call_kwargs["prior_context"] or ""
        assert design_output in prior_context, (
            "prior_context must still carry the FULL stage_output blob — the "
            "keyed store's section extraction must not alter prior_chunks"
        )
