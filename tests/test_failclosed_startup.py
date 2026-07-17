"""Tests for the fail-closed startup guard (plugin-arch-overhaul PRD, Sprint
01, acceptance criteria 2 & 3): `PipelineOrchestrator.run_pipeline` refuses to
start when NO agent runner kind is currently active in `RUNNER_MAP` (every
built-in agent flag off and no agent plugin registered), and proceeds
normally the moment at least one agent runner is active.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
from hivepilot.registry import AGENT_RUNNER_KINDS, NoAgentRunnerError, active_agent_runner_kinds

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_orchestrator.py's `_make_orchestrator_with_pipeline`)
# ---------------------------------------------------------------------------


def _make_pipeline() -> PipelineConfig:
    stages = [PipelineStage(name="stage-a", task="stage-a")]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(pipeline: PipelineConfig):
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
# active_agent_runner_kinds() sanity (also covered in test_gating_conformance.py;
# kept minimal here, focused on the guard's own inputs)
# ---------------------------------------------------------------------------


class TestActiveAgentRunnerKindsForGuard:
    def test_claude_only_registered_yields_nonempty(self) -> None:
        from hivepilot.registry import RUNNER_MAP

        RUNNER_MAP.clear()
        RUNNER_MAP["claude"] = object()
        assert active_agent_runner_kinds() == {"claude"}


# ---------------------------------------------------------------------------
# 2. Zero agent runners active -> run_pipeline raises NoAgentRunnerError
# ---------------------------------------------------------------------------


class TestNoAgentRunnerGuardRaises:
    def test_run_pipeline_raises_when_no_agent_runner_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = _make_pipeline()
        orch = _make_orchestrator_with_pipeline(pipeline)

        monkeypatch.setattr("hivepilot.registry.active_agent_runner_kinds", lambda: set())

        with patch("hivepilot.orchestrator.validate_pipeline", return_value=None):
            with pytest.raises(NoAgentRunnerError) as excinfo:
                orch.run_pipeline(
                    project_names=["proj"],
                    pipeline_name="test-pipe",
                    extra_prompt=None,
                    auto_git=False,
                    dry_run=True,
                )

        message = str(excinfo.value)
        # Message must name the enable-able kinds ...
        for kind in sorted(AGENT_RUNNER_KINDS):
            assert kind in message
        # ... and must leak nothing beyond kind names + the env-var hint.
        assert "secret" not in message.lower()
        assert "password" not in message.lower()
        assert "token" not in message.lower()
        assert "api_key" not in message.lower()

    def test_run_pipeline_never_calls_run_task_when_guard_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must trip BEFORE any stage executes -- no partial work."""
        pipeline = _make_pipeline()
        orch = _make_orchestrator_with_pipeline(pipeline)

        monkeypatch.setattr("hivepilot.registry.active_agent_runner_kinds", lambda: set())

        with (
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch.object(orch, "run_task") as mock_run_task,
        ):
            with pytest.raises(NoAgentRunnerError):
                orch.run_pipeline(
                    project_names=["proj"],
                    pipeline_name="test-pipe",
                    extra_prompt=None,
                    auto_git=False,
                    dry_run=True,
                )
        mock_run_task.assert_not_called()


# ---------------------------------------------------------------------------
# 3. At least one agent runner active -> guard passes, no raise
# ---------------------------------------------------------------------------


class TestNoAgentRunnerGuardPasses:
    def test_run_pipeline_does_not_raise_when_claude_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline()
        orch = _make_orchestrator_with_pipeline(pipeline)

        monkeypatch.setattr("hivepilot.registry.active_agent_runner_kinds", lambda: {"claude"})

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
            ),
        ):
            # Must not raise NoAgentRunnerError (or anything else here).
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
