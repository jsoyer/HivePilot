"""End-to-end integration tests for the plugin-arch-overhaul PRD (Sprint 05).

Unlike `tests/test_failclosed_startup.py` (which monkeypatches
`hivepilot.registry.active_agent_runner_kinds` directly to unit-test the
guard's own branch logic), these tests drive the REAL guard by mutating its
actual input data -- `hivepilot.registry.RUNNER_MAP` -- so the assertion
exercises the true code path: `Orchestrator.run_pipeline` ->
`_run_pipeline_body` -> `active_agent_runner_kinds()` (reading the real,
live `RUNNER_MAP`) -> `NoAgentRunnerError`. Nothing about the guard itself
is stubbed.

Scenarios:
  (a) A pipeline with only `claude` active in `RUNNER_MAP` runs to
      completion (guard passes, `run_task` gets invoked).
  (b) A pipeline with EVERY agent runner kind removed from `RUNNER_MAP`
      (built-ins + plugin agents) fails closed with `NoAgentRunnerError`,
      raised BEFORE any stage executes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
from hivepilot.registry import RUNNER_MAP
from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS


def _make_pipeline() -> PipelineConfig:
    stages = [PipelineStage(name="stage-a", task="stage-a")]
    return PipelineConfig(description="integration test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(pipeline: PipelineConfig):
    """Mirrors tests/test_failclosed_startup.py's helper: everything OUTSIDE
    the guard under test (project/task/pipeline loading, RunnerRegistry,
    PluginManager, validate_pipeline) is mocked so construction never touches
    real config files; the guard itself reads the real, live `RUNNER_MAP`."""
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"integration-pipe": pipeline})

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


class TestClaudeOnlyPipelineSucceeds:
    """(a) Real `RUNNER_MAP` state: ONLY `claude` present among agent kinds
    (every other agent kind -- built-in and plugin -- removed). The guard
    must NOT raise, and the pipeline must reach `run_task`."""

    def test_pipeline_runs_to_completion_with_only_claude_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.orchestrator import RunResult

        for kind in AGENT_RUNNER_KINDS:
            monkeypatch.delitem(RUNNER_MAP, kind, raising=False)
        monkeypatch.setitem(RUNNER_MAP, "claude", object())

        assert {k for k in RUNNER_MAP if k in AGENT_RUNNER_KINDS} == {"claude"}

        pipeline = _make_pipeline()
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ) as mock_run_task,
        ):
            results = orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="integration-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        assert results, "pipeline should have produced at least one RunResult"
        assert all(r.success for r in results)
        mock_run_task.assert_called()


class TestAllAgentsOffFailsClosed:
    """(b) Real `RUNNER_MAP` state: every agent runner kind removed (both
    built-ins -- claude/codex/vibe/openrouter -- and every optional plugin
    agent kind). `run_pipeline` must raise `NoAgentRunnerError` BEFORE any
    stage executes -- driven through the real entrypoint, not a stub of the
    guard function itself."""

    def test_run_pipeline_raises_no_agent_runner_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.registry import NoAgentRunnerError

        for kind in AGENT_RUNNER_KINDS:
            monkeypatch.delitem(RUNNER_MAP, kind, raising=False)
        assert not any(kind in RUNNER_MAP for kind in AGENT_RUNNER_KINDS)

        pipeline = _make_pipeline()
        orch = _make_orchestrator_with_pipeline(pipeline)

        with (
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task") as mock_run_task,
        ):
            with pytest.raises(NoAgentRunnerError):
                orch.run_pipeline(
                    project_names=["proj"],
                    pipeline_name="integration-pipe",
                    extra_prompt=None,
                    auto_git=False,
                    dry_run=True,
                )

        # Fail-closed: no partial work -- run_task must never have been
        # reached once the guard trips.
        mock_run_task.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
