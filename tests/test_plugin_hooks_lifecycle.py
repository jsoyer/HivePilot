"""
Tests for Sprint 4 (plugin system) — pipeline-lifecycle hooks.

Covers:
- `on_pipeline_start` fires once per `run_pipeline` call with run_id/pipeline/projects
- `on_pipeline_end` fires once per `run_pipeline` call (both success and fail-fast
  paths) with run_id/pipeline/status
- `on_error` fires when a stage fails without `continue_on_failure`, before the
  pipeline aborts
- a hook that raises is logged and does not propagate out of `run_pipeline`
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, MagicMock, patch

# hivepilot.orchestrator is imported at module level so it is in sys.modules
# before any patch("hivepilot.orchestrator.*") context managers are entered.
import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.models import PipelineConfig, PipelineStage
from hivepilot.plugins import PluginManager
from hivepilot.services.state_service import RunStatus

if TYPE_CHECKING:
    from hivepilot.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Records every call's kwargs — stands in for a plugin-contributed hook."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _bare_plugin_manager() -> PluginManager:
    """A real `PluginManager` instance without scanning `plugins/` (bypasses
    `__init__`'s filesystem/entry-point side effects) — so `run_hook` exercises
    the real accumulation/dispatch logic instead of a `MagicMock` no-op."""
    pm = PluginManager.__new__(PluginManager)
    pm.loaded = []
    pm.hooks = {"before_step": [], "after_step": []}
    pm.declared_notifiers = {}
    pm.plugins = []
    return pm


def _make_pipeline(*stage_names: str) -> PipelineConfig:
    stages = [PipelineStage(name=n, task=n) for n in stage_names]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(
    pipeline: PipelineConfig, plugin_manager: PluginManager | None = None
) -> "Orchestrator":
    """Return a minimal Orchestrator whose pipelines map contains only the given
    pipeline, with `orchestrator.plugins` set to a real (bare) `PluginManager`
    unless one is supplied — mirrors `test_pipeline_execution.py`'s helper."""
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})
    pm = plugin_manager if plugin_manager is not None else _bare_plugin_manager()

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=pm),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        orch = Orchestrator()

    return orch


# ---------------------------------------------------------------------------
# on_pipeline_start / on_pipeline_end — success path
# ---------------------------------------------------------------------------


class TestPipelineStartEndHooksFireOnSuccess:
    def test_on_pipeline_start_and_end_fire_with_expected_kwargs(self) -> None:
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a")
        pm = _bare_plugin_manager()
        start_recorder = _Recorder()
        end_recorder = _Recorder()
        pm.hooks["on_pipeline_start"] = [start_recorder]
        pm.hooks["on_pipeline_end"] = [end_recorder]

        orch = _make_orchestrator_with_pipeline(pipeline, plugin_manager=pm)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=11),
            patch("hivepilot.orchestrator.state_service.complete_run"),
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

        assert len(start_recorder.calls) == 1, start_recorder.calls
        assert start_recorder.calls[0]["run_id"] == 11
        assert start_recorder.calls[0]["pipeline"] == "test-pipe"
        assert start_recorder.calls[0]["projects"] == ["proj"]

        assert len(end_recorder.calls) == 1, end_recorder.calls
        assert end_recorder.calls[0]["run_id"] == 11
        assert end_recorder.calls[0]["pipeline"] == "test-pipe"
        assert end_recorder.calls[0]["status"] == RunStatus.COMPLETE.value


# ---------------------------------------------------------------------------
# on_error — fail-fast path
# ---------------------------------------------------------------------------


class TestOnErrorHookFiresOnFailFast:
    def test_on_error_fires_before_abort_and_pipeline_end_still_fires(self) -> None:
        """A failing stage (no continue_on_failure) fires on_error with the
        failing stage's name, and on_pipeline_end still fires afterwards with
        the final TEST_FAILURE status — both hooks fire exactly once."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a", "stage-b")
        pm = _bare_plugin_manager()
        error_recorder = _Recorder()
        end_recorder = _Recorder()
        pm.hooks["on_error"] = [error_recorder]
        pm.hooks["on_pipeline_end"] = [end_recorder]

        orch = _make_orchestrator_with_pipeline(pipeline, plugin_manager=pm)

        run_task_calls: list[str] = []

        def _run_task_fail_first(**kwargs: object) -> list[RunResult]:
            task_name = str(kwargs["task_name"])
            run_task_calls.append(task_name)
            if task_name == "stage-a":
                return [RunResult("proj", task_name, False, "boom")]
            return [RunResult("proj", task_name, True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=22),
            patch("hivepilot.orchestrator.state_service.complete_run"),
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

        assert "stage-b" not in run_task_calls, run_task_calls

        assert len(error_recorder.calls) == 1, error_recorder.calls
        assert error_recorder.calls[0]["run_id"] == 22
        assert error_recorder.calls[0]["pipeline"] == "test-pipe"
        assert error_recorder.calls[0]["stage"] == "stage-a"

        assert len(end_recorder.calls) == 1, end_recorder.calls
        assert end_recorder.calls[0]["status"] == RunStatus.TEST_FAILURE.value


# ---------------------------------------------------------------------------
# Broken hook isolation
# ---------------------------------------------------------------------------


class TestBrokenHookDoesNotCrashPipeline:
    def test_raising_hook_is_logged_and_run_pipeline_completes(self) -> None:
        """A hook that raises must be caught, logged, and never propagate —
        `run_pipeline` returns normally instead of raising."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline("stage-a")
        pm = _bare_plugin_manager()

        def _raising_hook(**kwargs: Any) -> None:
            raise RuntimeError("broken plugin hook")

        pm.hooks["on_pipeline_start"] = [_raising_hook]

        orch = _make_orchestrator_with_pipeline(pipeline, plugin_manager=pm)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=33),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
            patch("hivepilot.orchestrator.logger") as mock_logger,
        ):
            # Must not raise — a broken plugin hook is best-effort/isolated.
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        mock_logger.warning.assert_any_call(
            "plugins.hook_failed", hook="on_pipeline_start", run_id=33, error=ANY
        )
