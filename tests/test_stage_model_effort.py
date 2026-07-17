"""Sprint 1 (roles-model-effort-config-owned PRD): stage `model`/`effort` +
resolution precedence + effort propagation.

Covers:
- `PipelineStage`/`PipelineConfig`/`RunnerDefinition` accept `model`/`effort`;
  an invalid `effort` value is rejected by Pydantic.
- `resolve_stage_model`/`resolve_effort` (models.py): stage-over-pipeline
  precedence, mirroring `resolve_mode`.
- `hivepilot.roles.resolve_stage_dispatch`: full `policy > stage > role >
  runner-default` precedence, `allowed_runners` fail-closed enforcement, and
  `resolve_runner` byte-identical delegation.
- Effort propagation into runners: `CodexRunner` builds
  `-c model_reasoning_effort=<level>` (defaulting to `"medium"` when unset —
  byte-identical to the pre-Sprint-1 hardcoded tuple); `ClaudeRunner` and
  other prompt-cli runners treat effort as a documented no-op (never crash).
- Orchestrator dispatch: a stage that sets neither `model` nor `effort`
  dispatches byte-identically to before these fields existed; a stage that
  DOES set them propagates into the runner definition actually used.
- The dual-model debate trigger (`len(role.models) > 1`) is unaffected by the
  new `stage_model`/`stage_effort` params.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import (
    EffortLevel,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskConfig,
    TaskStep,
    resolve_effort,
    resolve_stage_model,
)
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.prompt_cli_runner import CodexRunner, GeminiRunner
from hivepilot.services.policy_service import Policy

# ---------------------------------------------------------------------------
# Field validation: PipelineStage / PipelineConfig / RunnerDefinition
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_pipeline_stage_model_effort_default_to_none(self) -> None:
        stage = PipelineStage(name="s", task="t")
        assert stage.model is None
        assert stage.effort is None

    def test_pipeline_stage_accepts_model_and_effort(self) -> None:
        stage = PipelineStage(name="s", task="t", model="gpt-5.5", effort="high")
        assert stage.model == "gpt-5.5"
        assert stage.effort == "high"

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_pipeline_stage_accepts_every_effort_level(self, level: EffortLevel) -> None:
        assert PipelineStage(name="s", task="t", effort=level).effort == level

    def test_pipeline_stage_rejects_invalid_effort(self) -> None:
        with pytest.raises(ValueError):
            PipelineStage(name="s", task="t", effort="extreme")  # type: ignore[arg-type]

    def test_pipeline_config_model_effort_default_to_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        assert pipeline.model is None
        assert pipeline.effort is None

    def test_pipeline_config_accepts_model_and_effort(self) -> None:
        pipeline = PipelineConfig(description="d", model="gpt-5.5", effort="low")
        assert pipeline.model == "gpt-5.5"
        assert pipeline.effort == "low"

    def test_pipeline_config_rejects_invalid_effort(self) -> None:
        with pytest.raises(ValueError):
            PipelineConfig(description="d", effort="extreme")  # type: ignore[arg-type]

    def test_runner_definition_effort_defaults_to_none(self) -> None:
        assert RunnerDefinition(kind="codex").effort is None

    def test_runner_definition_accepts_effort(self) -> None:
        assert RunnerDefinition(kind="codex", effort="xhigh").effort == "xhigh"

    def test_runner_definition_rejects_invalid_effort(self) -> None:
        with pytest.raises(ValueError):
            RunnerDefinition(kind="codex", effort="extreme")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_stage_model / resolve_effort (models.py) — stage over pipeline
# ---------------------------------------------------------------------------


class TestResolveStageModelAndEffort:
    def test_resolve_stage_model_defaults_to_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        stage = PipelineStage(name="s", task="t")
        assert resolve_stage_model(pipeline, stage) is None

    def test_resolve_stage_model_pipeline_over_default(self) -> None:
        pipeline = PipelineConfig(description="d", model="pipeline-model")
        stage = PipelineStage(name="s", task="t")
        assert resolve_stage_model(pipeline, stage) == "pipeline-model"

    def test_resolve_stage_model_stage_over_pipeline(self) -> None:
        pipeline = PipelineConfig(description="d", model="pipeline-model")
        stage = PipelineStage(name="s", task="t", model="stage-model")
        assert resolve_stage_model(pipeline, stage) == "stage-model"

    def test_resolve_effort_defaults_to_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        stage = PipelineStage(name="s", task="t")
        assert resolve_effort(pipeline, stage) is None

    def test_resolve_effort_pipeline_over_default(self) -> None:
        pipeline = PipelineConfig(description="d", effort="low")
        stage = PipelineStage(name="s", task="t")
        assert resolve_effort(pipeline, stage) == "low"

    def test_resolve_effort_stage_over_pipeline(self) -> None:
        pipeline = PipelineConfig(description="d", effort="low")
        stage = PipelineStage(name="s", task="t", effort="high")
        assert resolve_effort(pipeline, stage) == "high"


# ---------------------------------------------------------------------------
# resolve_stage_dispatch — policy > stage > role > runner-default precedence
# ---------------------------------------------------------------------------


class TestResolveStageDispatchPrecedence:
    def test_role_default_when_no_stage_no_policy(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        runner, model, effort = resolve_stage_dispatch("developer")
        assert runner == "claude"
        assert model is None  # developer role sets no explicit model
        assert effort is None

    def test_stage_model_overrides_role_default(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        runner, model, effort = resolve_stage_dispatch("developer", stage_model="claude-opus")
        assert runner == "claude"
        assert model == "claude-opus"
        assert effort is None

    def test_stage_effort_overrides_role_default_none(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        runner, model, effort = resolve_stage_dispatch("developer", stage_effort="high")
        assert effort == "high"

    def test_policy_wins_over_stage_model(self) -> None:
        """A policy `role_overrides` entry must NEVER be short-circuited by a
        stage-level model — policy is the security control."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"model": "policy-model"}})
        runner, model, effort = resolve_stage_dispatch(
            "developer", policy, stage_model="stage-model"
        )
        assert model == "policy-model"
        assert runner == "claude"
        assert effort is None

    def test_policy_wins_over_stage_effort(self) -> None:
        """Same policy-outranks-stage guarantee for `effort`."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"effort": "low"}})
        runner, model, effort = resolve_stage_dispatch("developer", policy, stage_effort="max")
        assert effort == "low"

    def test_policy_runner_override_still_applies_with_stage_model_set(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"runner": "codex"}})
        runner, model, effort = resolve_stage_dispatch(
            "developer", policy, stage_model="stage-model"
        )
        assert runner == "codex"
        assert model == "stage-model"

    def test_allowed_runners_fails_closed_even_with_stage_override(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(allowed_runners=["opencode"])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_stage_dispatch("developer", policy, stage_model="whatever")

    def test_allowed_runners_fails_closed_with_no_stage_override(self) -> None:
        """Same fail-closed guarantee on the no-stage delegate-to-resolve_runner
        path (stage_model/stage_effort both None)."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(allowed_runners=["opencode"])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_stage_dispatch("developer", policy)

    def test_no_stage_args_matches_resolve_runner_exactly(self) -> None:
        """`resolve_stage_dispatch(role, policy)` with no stage args must
        return the exact same (runner, model) `resolve_runner` returns — the
        byte-identical "stage sets nothing" contract."""
        from hivepilot.roles import resolve_runner, resolve_stage_dispatch

        policy = Policy(role_overrides={"reviewer": {"model": "gpt-6"}})
        expected_runner, expected_model = resolve_runner("reviewer", policy)
        runner, model, _effort = resolve_stage_dispatch("reviewer", policy)
        assert (runner, model) == (expected_runner, expected_model)

    def test_resolve_runner_unaffected_by_this_sprint(self) -> None:
        """`resolve_runner` itself (used by callers with no stage context,
        e.g. the dual-model debate path) must be untouched."""
        from hivepilot.roles import resolve_runner

        runner, model = resolve_runner("reviewer")
        assert runner == "codex"
        assert model == "gpt-5.5"


# ---------------------------------------------------------------------------
# Effort propagation — CodexRunner / ClaudeRunner / other prompt-cli runners
# ---------------------------------------------------------------------------


def _payload(tmp_path: Path, step_metadata: dict | None = None) -> RunnerPayload:
    """*step_metadata* lands on ``step.metadata`` — the channel
    ``resolve_runner_effort``/``_build_cli_args`` (model) actually read a
    per-step override from, NOT ``payload.metadata`` (that's the
    extra_prompt/prior_context channel)."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(
            name="s", runner="x", prompt_file=str(prompt_file), metadata=step_metadata or {}
        ),
        metadata={},
        secrets={},
    )


class TestCodexEffortPropagation:
    def test_default_medium_when_effort_unset_byte_identical(self, tmp_path: Path) -> None:
        """Byte-identical to the pre-Sprint-1 hardcoded
        `cli_flags = ("-c", "model_reasoning_effort=medium")` tuple."""
        from hivepilot.config import settings

        runner = CodexRunner(RunnerDefinition(kind="codex", command="codex"), settings)
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert args[:2] == ["codex", "exec"]
        assert "-c" in args
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=medium"
        assert args[-1] == "do the thing"

    def test_definition_effort_is_used(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", effort="high"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=high"

    def test_step_metadata_effort_overrides_definition(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", effort="low"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path, step_metadata={"effort": "xhigh"}))
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=xhigh"


class TestNonEffortRunnersIgnoreEffortSafely:
    def test_claude_runner_never_crashes_and_emits_no_effort_flag(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = ClaudeRunner(
            RunnerDefinition(kind="claude", command="claude", effort="max"), settings
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert not any("effort" in str(a).lower() for a in args)

    def test_gemini_runner_never_crashes_with_effort_set(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = GeminiRunner(
            RunnerDefinition(kind="gemini", command="gemini", effort="max"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert not any("effort" in str(a).lower() for a in args)


# ---------------------------------------------------------------------------
# Orchestrator dispatch — byte-identical when stage sets neither field,
# propagated when it does.
# ---------------------------------------------------------------------------


def _bare_orchestrator():
    from hivepilot.orchestrator import Orchestrator

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(
            "hivepilot.orchestrator.load_pipelines",
            return_value=MagicMock(pipelines={}),
        ),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    orch.plugins = MagicMock()
    return orch


class TestOrchestratorStageDispatchByteIdentical:
    def test_stage_unset_dispatches_byte_identically(self, tmp_path: Path) -> None:
        """A role-driven task run with no stage_model/stage_effort (the
        plain `run_task` default) must build the EXACT same RunnerDefinition
        (model=None, effort=None) as before these fields existed."""
        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "ok"
        task = TaskConfig(
            description="dev",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="developer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
            )
        called_def = orch.registry.capture_definition.call_args.args[0]
        assert called_def.model is None
        assert called_def.effort is None
        assert called_def.kind == "claude"

    def test_stage_model_and_effort_propagate_into_runner_definition(self, tmp_path: Path) -> None:
        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "ok"
        task = TaskConfig(
            description="dev",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="developer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
                stage_model="claude-opus-x",
                stage_effort="high",
            )
        called_def = orch.registry.capture_definition.call_args.args[0]
        assert called_def.model == "claude-opus-x"
        assert called_def.effort == "high"

    def test_codex_default_medium_byte_identical_through_run_task(self, tmp_path: Path) -> None:
        """End-to-end: a `reviewer` (codex) role step with no stage overrides
        must still resolve to `medium` effort via the real CodexRunner path."""
        from hivepilot.registry import RunnerRegistry

        orch = _bare_orchestrator()
        orch.registry = RunnerRegistry({})
        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("review this", encoding="utf-8")
        task = TaskConfig(
            description="review",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="codex", prompt_file=str(prompt_file))],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            orch._execute_task(
                project=project,
                task_name="reviewer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
            )
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=medium"


# ---------------------------------------------------------------------------
# Dual-model debate path preserved — stage_model/stage_effort never
# short-circuit `len(role.models) > 1`.
# ---------------------------------------------------------------------------


class TestDebatePathPreservedWithStageParams:
    def test_dual_model_role_still_triggers_debate_with_stage_params(self, tmp_path: Path) -> None:
        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        task = TaskConfig(
            description="intake",
            role="ceo",
            engine="native",
            steps=[TaskStep(name="s", runner="opencode", prompt_file="p.md")],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "run_debate") as mock_debate,
        ):
            orch._execute_task(
                project=project,
                task_name="ceo-intake",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=True,
                dry_run=True,
                stage_model="some-model",
                stage_effort="high",
            )
        mock_debate.assert_called_once()
        assert mock_debate.call_args.kwargs["role_name"] == "ceo"
        orch.registry.execute_definition.assert_not_called()
