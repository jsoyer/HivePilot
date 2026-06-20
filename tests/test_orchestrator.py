"""
Tests for Sprint 2.6 — Documentation agent active.

Covers:
- 2.6a: per-stage interaction logging in run_pipeline
- 2.6b: tasks.yaml company-documentation step uses gemini runner/runner_ref
- 2.6c: documentation stage writes a vault note via ObsidianService.write_note
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.models import PipelineConfig, PipelineStage

# ---------------------------------------------------------------------------
# Helpers (mirrors test_pipeline_execution.py)
# ---------------------------------------------------------------------------


def _make_pipeline(*stage_defs: tuple[str, str]) -> PipelineConfig:
    """Build a PipelineConfig with (stage_name, task_name) tuples."""
    stages = [PipelineStage(name=name, task=task) for name, task in stage_defs]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_pipeline_by_name(*stage_names: str) -> PipelineConfig:
    """Build a PipelineConfig with stage name == task name (convenience)."""
    stages = [PipelineStage(name=n, task=n) for n in stage_names]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(pipeline: PipelineConfig):
    """Return a minimal Orchestrator with the given pipeline (mirrors test_pipeline_execution.py)."""
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
# 2.6a — per-stage interaction logging
# ---------------------------------------------------------------------------


class TestPerStageInteractionLogging:
    """InteractionService.log_interaction is called once per stage with correct actor/target."""

    def test_log_interaction_called_once_per_stage(self) -> None:
        """Two-stage pipeline → log_interaction called twice, once per stage."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_by_name("stage-a", "stage-b")
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=42),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
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

        assert mock_svc.log_interaction.call_count == 2, (
            f"Expected 2 log_interaction calls (one per stage), got {mock_svc.log_interaction.call_count}"
        )

    def test_actor_is_stage_name(self) -> None:
        """Each interaction's actor matches the stage name."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_by_name("ceo-intake", "cos-plan")
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=7),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
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

        calls = mock_svc.log_interaction.call_args_list
        actors = [c.args[0].actor for c in calls]
        assert actors == ["ceo-intake", "cos-plan"], (
            f"Expected actors ['ceo-intake', 'cos-plan'], got: {actors}"
        )

    def test_target_is_next_stage_name_or_none(self) -> None:
        """First stage targets the second; last stage targets None."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_by_name("alpha", "beta", "gamma")
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=3),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
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

        calls = mock_svc.log_interaction.call_args_list
        targets = [c.args[0].target for c in calls]
        assert targets == ["beta", "gamma", None], (
            f"Expected targets ['beta', 'gamma', None], got: {targets}"
        )

    def test_interaction_carries_run_id_and_pipeline_metadata(self) -> None:
        """Logged interactions carry run_id and pipeline in metadata."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_by_name("stage-x")
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=99),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
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

        interaction_arg = mock_svc.log_interaction.call_args_list[0].args[0]
        assert interaction_arg.run_id == 99, f"Expected run_id=99, got {interaction_arg.run_id}"
        assert interaction_arg.metadata is not None
        assert interaction_arg.metadata.get("pipeline") == "test-pipe", (
            f"Expected pipeline='test-pipe' in metadata, got: {interaction_arg.metadata}"
        )

    def test_log_interaction_called_even_on_failing_stage(self) -> None:
        """Interaction is logged after a failing stage (before fail-fast break)."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline_by_name("stage-fail", "stage-skip")
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_svc = MagicMock()

        def _run_task_fail(**kwargs):
            task = kwargs["task_name"]
            if task == "stage-fail":
                return [RunResult("proj", task, False, "boom")]
            return [RunResult("proj", task, True)]

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=5),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
            patch.object(orch, "run_task", side_effect=_run_task_fail),
        ):
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        # stage-fail ran and was logged; stage-skip was NOT reached (fail-fast)
        assert mock_svc.log_interaction.call_count == 1, (
            f"Expected exactly 1 log_interaction call (for stage-fail), got {mock_svc.log_interaction.call_count}"
        )


# ---------------------------------------------------------------------------
# 2.6c — documentation stage writes vault note
# ---------------------------------------------------------------------------


class TestDocumentationVaultWrite:
    """Documentation stage triggers ObsidianService.write_note with correct args."""

    def test_write_note_called_for_doc_stage_with_vault(self, tmp_path: Path) -> None:
        """When task == 'company-documentation' and vault_path exists, write_note is called."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline(("doc-stage", "company-documentation"))
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_obs = MagicMock()
        mock_obs_class = MagicMock(return_value=mock_obs)
        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=20),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
            patch("hivepilot.orchestrator.ObsidianService", mock_obs_class),
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [
                    RunResult("proj", kwargs["task_name"], True, "doc output")
                ],
            ),
        ):
            mock_settings.obsidian_vault = tmp_path  # exists → vault_path set
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        mock_obs.write_note.assert_called_once()
        call_kwargs = mock_obs.write_note.call_args

        # subpath starts with "Docs/"
        subpath = call_kwargs.kwargs.get("subpath") or call_kwargs.args[0]
        assert subpath.startswith("Docs/"), (
            f"Expected subpath to start with 'Docs/', got: {subpath!r}"
        )

        # frontmatter type == "documentation"
        frontmatter = call_kwargs.kwargs.get("frontmatter_fields") or call_kwargs.args[3]
        assert frontmatter.get("type") == "documentation", (
            f"Expected type='documentation' in frontmatter, got: {frontmatter}"
        )

    def test_write_note_not_called_for_non_doc_stage(self, tmp_path: Path) -> None:
        """Non-documentation stages must NOT trigger write_note."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline(
            ("ceo-stage", "company-ceo-intake"),
            ("cto-stage", "company-cto-review"),
        )
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_obs = MagicMock()
        mock_obs_class = MagicMock(return_value=mock_obs)
        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=21),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
            patch("hivepilot.orchestrator.ObsidianService", mock_obs_class),
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
        ):
            mock_settings.obsidian_vault = tmp_path
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        mock_obs.write_note.assert_not_called()

    def test_write_note_not_called_when_vault_path_is_none(self) -> None:
        """When vault_path is None (vault doesn't exist), write_note must not be called."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline(("doc-stage", "company-documentation"))
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_obs = MagicMock()
        mock_obs_class = MagicMock(return_value=mock_obs)
        mock_svc = MagicMock()

        # Use a non-existent path so vault_path resolves to None
        non_existent = Path("/tmp/no_such_vault_xyz_12345")

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=22),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
            patch("hivepilot.orchestrator.ObsidianService", mock_obs_class),
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [RunResult("proj", kwargs["task_name"], True)],
            ),
        ):
            mock_settings.obsidian_vault = non_existent  # .exists() → False → vault_path = None
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        mock_obs.write_note.assert_not_called()

    def test_write_note_frontmatter_has_run_id_and_pipeline(self, tmp_path: Path) -> None:
        """Frontmatter includes run_id and pipeline fields."""
        from hivepilot.orchestrator import RunResult

        pipeline = _make_pipeline(("doc-stage", "company-documentation"))
        orch = _make_orchestrator_with_pipeline(pipeline)

        mock_obs = MagicMock()
        mock_obs_class = MagicMock(return_value=mock_obs)
        mock_svc = MagicMock()

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=55),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=mock_svc),
            patch("hivepilot.orchestrator.ObsidianService", mock_obs_class),
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch.object(
                orch,
                "run_task",
                side_effect=lambda **kwargs: [
                    RunResult("proj", kwargs["task_name"], True, "docs updated")
                ],
            ),
        ):
            mock_settings.obsidian_vault = tmp_path
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        call_kwargs = mock_obs.write_note.call_args
        frontmatter = call_kwargs.kwargs.get("frontmatter_fields") or call_kwargs.args[3]

        assert frontmatter.get("run_id") == 55, (
            f"Expected run_id=55 in frontmatter, got: {frontmatter.get('run_id')}"
        )
        assert frontmatter.get("pipeline") == "test-pipe", (
            f"Expected pipeline='test-pipe' in frontmatter, got: {frontmatter.get('pipeline')}"
        )
        assert frontmatter.get("agent") == "gemini-cli", (
            f"Expected agent='gemini-cli' in frontmatter, got: {frontmatter.get('agent')}"
        )


# ---------------------------------------------------------------------------
# 2.6b — tasks.yaml runner binding for company-documentation
# ---------------------------------------------------------------------------


class TestTasksYamlDocumentationBinding:
    """company-documentation task step must use gemini runner and gemini-cli runner_ref."""

    def test_company_documentation_runner_is_gemini(self) -> None:
        """company-documentation step.runner == 'gemini'."""
        from hivepilot.services.project_service import load_tasks

        tasks_config = load_tasks()
        task = tasks_config.tasks.get("company-documentation")
        assert task is not None, "company-documentation task not found in tasks.yaml"
        assert task.steps, "company-documentation task has no steps"

        step = task.steps[0]
        assert step.runner == "gemini", f"Expected runner='gemini', got: {step.runner!r}"

    def test_company_documentation_runner_ref_is_gemini_cli(self) -> None:
        """company-documentation step.runner_ref == 'gemini-cli'."""
        from hivepilot.services.project_service import load_tasks

        tasks_config = load_tasks()
        task = tasks_config.tasks.get("company-documentation")
        assert task is not None, "company-documentation task not found in tasks.yaml"
        step = task.steps[0]

        assert step.runner_ref == "gemini-cli", (
            f"Expected runner_ref='gemini-cli', got: {step.runner_ref!r}"
        )

    def test_company_documentation_other_fields_unchanged(self) -> None:
        """Switching to gemini runner must not alter other step fields."""
        from hivepilot.services.project_service import load_tasks

        tasks_config = load_tasks()
        task = tasks_config.tasks.get("company-documentation")
        assert task is not None
        step = task.steps[0]

        assert step.prompt_file == "prompts/agents/documentation.md", (
            f"prompt_file changed unexpectedly: {step.prompt_file!r}"
        )
        assert step.timeout_seconds == 3600, (
            f"timeout_seconds changed unexpectedly: {step.timeout_seconds}"
        )


# ---------------------------------------------------------------------------
# Simulate mode (item 5) — exercise wiring without invoking real runners
# ---------------------------------------------------------------------------


class TestSimulateMode:
    def test_execute_task_simulate_skips_runner(self) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        task = TaskConfig(
            description="t", engine="native", steps=[TaskStep(name="s", runner="claude")]
        )
        project = ProjectConfig(path=Path("/tmp/simproj"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=True,
            )
        orch.registry.execute.assert_not_called()
        # step is still recorded as success (simulated)
        mock_step.assert_called_with(1, "s", "success")

    def test_run_pipeline_forwards_simulate_to_run_task(self) -> None:
        pipeline = _make_pipeline_by_name("stage-a")
        orch = _make_orchestrator_with_pipeline(pipeline)
        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=7),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
            patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
            patch("hivepilot.orchestrator.InteractionService", return_value=MagicMock()),
            patch.object(orch, "run_task", return_value=[]) as mock_run_task,
        ):
            orch.run_pipeline(
                project_names=["p"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                simulate=True,
            )
        assert mock_run_task.call_args.kwargs.get("simulate") is True


# ---------------------------------------------------------------------------
# Role-driven execution (item A) — task.role resolves runner+model via roles.py
# + per-project policy overrides
# ---------------------------------------------------------------------------


class TestRoleDrivenExecution:
    def _run(self, role: str, policy=None):
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        task = TaskConfig(
            description="t",
            role=role,
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                policy=policy,
            )
        return orch

    def test_role_resolves_runner_and_model(self) -> None:
        # reviewer is single-model (cto/ciso are now bi-modal → debate path)
        orch = self._run("reviewer")
        orch.registry.execute_definition.assert_called_once()
        orch.registry.execute.assert_not_called()
        rdef = orch.registry.execute_definition.call_args.args[0]
        assert rdef.kind == "codex"
        assert rdef.model == "gpt-5.5"

    def test_policy_override_changes_model(self) -> None:
        from hivepilot.services.policy_service import Policy

        orch = self._run("reviewer", policy=Policy(role_overrides={"reviewer": {"model": "glm"}}))
        rdef = orch.registry.execute_definition.call_args.args[0]
        assert rdef.kind == "codex"
        assert rdef.model == "glm"

    def test_allowed_runners_blocks_disallowed(self) -> None:
        import pytest

        from hivepilot.services.policy_service import Policy

        with pytest.raises(RuntimeError):
            self._run("reviewer", policy=Policy(allowed_runners=["opencode", "claude"]))


# ---------------------------------------------------------------------------
# Dual-model debate (item B) — DebateService wired & reachable
# ---------------------------------------------------------------------------


class TestDebate:
    def test_run_debate_one_position_per_model(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda step: {})

        captured: dict = {}

        class FakeDebate:
            def __init__(self, vault, dry_run=True):
                pass

            def run(self, topic, positions, decision=None, **kw):
                captured["positions"] = positions
                captured["decision"] = decision
                return {"path": "ADR.md", "dry_run": True}

        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", FakeDebate)
        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p", role_name="ceo", topic="adopt X?", simulate=True
            )

        assert adr == {"path": "ADR.md", "dry_run": True}
        assert {pos.role for pos in captured["positions"]} == {
            "ceo:opencode-go/qwen3.7-max",
            "ceo:opencode-go/kimi-k2.6",
        }
        orch.registry.capture_definition.assert_not_called()  # simulate -> no real calls

    def test_run_debate_rejects_single_model_role(self) -> None:
        import pytest

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        with pytest.raises(ValueError):
            orch.run_debate(project_name="p", role_name="developer", topic="x", simulate=True)


class TestDebateAutoTrigger:
    def test_dual_model_role_task_triggers_debate(self) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        task = TaskConfig(
            description="intake",
            role="ceo",
            engine="native",
            steps=[TaskStep(name="s", runner="opencode", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "run_debate") as mock_debate,
        ):
            orch._execute_task(
                project=project,
                task_name="company-ceo-intake",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=True,
                dry_run=True,
            )
        mock_debate.assert_called_once()
        assert mock_debate.call_args.kwargs["role_name"] == "ceo"
        orch.registry.execute_definition.assert_not_called()  # debate path returns early

    def test_single_model_role_task_does_not_trigger_debate(self) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        task = TaskConfig(
            description="dev",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "run_debate") as mock_debate,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="company-developer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=True,
            )
        mock_debate.assert_not_called()
