"""
Tests for Sprint 2.6 — Documentation agent active.

Covers:
- 2.6a: per-stage interaction logging in run_pipeline
- 2.6b: tasks.yaml documentation step uses gemini runner/runner_ref
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
        """When commits_vault is True and vault_path exists, write_note is called."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="test pipeline",
            stages=[PipelineStage(name="doc-stage", task="documentation", commits_vault=True)],
        )
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
            mock_settings.enable_challenge_rounds = False
            mock_settings.enable_agent_requests = False
            mock_settings.max_requests_per_run = 20
            mock_settings.prior_context_mode = "cap"
            mock_settings.max_prior_context_chars = 8000
            mock_settings.auditor_auto = False
            mock_settings.auto_commit_vault = False
            mock_settings.event_webhook_url = None
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
            ("ceo-stage", "ceo-intake"),
            ("cto-stage", "cto-review"),
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
            mock_settings.enable_challenge_rounds = False
            mock_settings.enable_agent_requests = False
            mock_settings.max_requests_per_run = 20
            mock_settings.prior_context_mode = "cap"
            mock_settings.max_prior_context_chars = 8000
            mock_settings.auditor_auto = False
            mock_settings.auto_commit_vault = False
            mock_settings.event_webhook_url = None
            orch.run_pipeline(
                project_names=["proj"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                dry_run=True,
            )

        mock_obs.write_note.assert_not_called()

    def test_write_note_not_called_when_vault_path_is_none(self) -> None:
        """When commits_vault is True but vault_path is None (vault doesn't exist), write_note must not be called."""
        from hivepilot.orchestrator import RunResult

        pipeline = PipelineConfig(
            description="test pipeline",
            stages=[PipelineStage(name="doc-stage", task="documentation", commits_vault=True)],
        )
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
            mock_settings.enable_challenge_rounds = False
            mock_settings.enable_agent_requests = False
            mock_settings.max_requests_per_run = 20
            mock_settings.prior_context_mode = "cap"
            mock_settings.max_prior_context_chars = 8000
            mock_settings.auditor_auto = False
            mock_settings.auto_commit_vault = False
            mock_settings.event_webhook_url = None
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

        pipeline = PipelineConfig(
            description="test pipeline",
            stages=[PipelineStage(name="doc-stage", task="documentation", commits_vault=True)],
        )
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
            mock_settings.enable_challenge_rounds = False
            mock_settings.enable_agent_requests = False
            mock_settings.max_requests_per_run = 20
            mock_settings.prior_context_mode = "cap"
            mock_settings.max_prior_context_chars = 8000
            mock_settings.auditor_auto = False
            mock_settings.auto_commit_vault = False
            mock_settings.event_webhook_url = None
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
# 2.6b — tasks.yaml runner binding for documentation
# ---------------------------------------------------------------------------


class TestTasksYamlDocumentationBinding:
    """documentation task step must use gemini runner and gemini-cli runner_ref."""

    def test_company_documentation_runner_is_gemini(self) -> None:
        """documentation step.runner == 'gemini'."""
        from hivepilot.services.project_service import load_tasks

        tasks_config = load_tasks()
        task = tasks_config.tasks.get("documentation")
        assert task is not None, "documentation task not found in tasks.yaml"
        assert task.steps, "documentation task has no steps"

        step = task.steps[0]
        assert step.runner == "gemini", f"Expected runner='gemini', got: {step.runner!r}"

    def test_company_documentation_runner_ref_is_gemini_cli(self) -> None:
        """documentation step.runner_ref == 'gemini-cli'."""
        from hivepilot.services.project_service import load_tasks

        tasks_config = load_tasks()
        task = tasks_config.tasks.get("documentation")
        assert task is not None, "documentation task not found in tasks.yaml"
        step = task.steps[0]

        assert step.runner_ref == "gemini-cli", (
            f"Expected runner_ref='gemini-cli', got: {step.runner_ref!r}"
        )

    def test_company_documentation_other_fields_unchanged(self) -> None:
        """Switching to gemini runner must not alter other step fields."""
        from hivepilot.services.project_service import load_tasks

        tasks_config = load_tasks()
        task = tasks_config.tasks.get("documentation")
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
        # step is still recorded as success (simulated). registry is a bare
        # MagicMock here so runner_def.kind/.model aren't real strings — this
        # test only cares about the (run_id, step, status) triple, not the
        # Phase 24b.1 provider/model kwargs (covered by
        # TestStepProviderModelThreading with a real RunnerDefinition).
        assert mock_step.call_args.args[:3] == (1, "s", "success")

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
        orch.registry.capture_definition.return_value = "agent output"
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
        orch.registry.capture_definition.assert_called_once()  # output is captured + surfaced
        orch.registry.execute.assert_not_called()
        rdef = orch.registry.capture_definition.call_args.args[0]
        assert rdef.kind == "codex"
        assert rdef.model == "gpt-5.5"

    def test_policy_override_changes_model(self) -> None:
        from hivepilot.services.policy_service import Policy

        orch = self._run("reviewer", policy=Policy(role_overrides={"reviewer": {"model": "glm"}}))
        rdef = orch.registry.capture_definition.call_args.args[0]
        assert rdef.kind == "codex"
        assert rdef.model == "glm"

    def test_allowed_runners_blocks_disallowed(self) -> None:
        import pytest

        from hivepilot.services.policy_service import Policy

        with pytest.raises(RuntimeError):
            self._run("reviewer", policy=Policy(allowed_runners=["opencode", "claude"]))


# ---------------------------------------------------------------------------
# Phase 24b.1 — orchestrator threads the resolved provider/model into
# state_service.record_step for each step.
# ---------------------------------------------------------------------------


class TestStepProviderModelThreading:
    def test_role_step_success_records_provider_and_model(self) -> None:
        """reviewer resolves to kind='codex', model='gpt-5.5' (same fixture
        as TestRoleDrivenExecution) — record_step must receive both."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "agent output"
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
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
            )

        mock_step.assert_called_once_with(1, "s", "success", provider="codex", model="gpt-5.5")

    def test_role_step_failure_records_provider_and_model(self) -> None:
        """A failed step must still thread provider/model — the runner that
        was actually attempted is known even though it raised."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = RuntimeError("boom")
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md", allow_failure=True)],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
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
            )

        mock_step.assert_called_once_with(
            1, "s", "failed", "boom", provider="codex", model="gpt-5.5"
        )

    def test_fallback_records_the_runner_that_actually_succeeded(self) -> None:
        """Quota fallback (developer role): claude -> codex. record_step must
        reflect the FALLBACK runner (codex), not the originally-resolved one."""
        from hivepilot.models import GitActions, ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()

        def capture_side_effect(runner_def, payload):
            if runner_def.kind == "claude":
                raise RuntimeError(
                    "claude exited 1: You've hit your session limit · resets 9:40pm (Europe/Paris)"
                )
            return "codex output"

        orch.registry.capture_definition.side_effect = capture_side_effect
        task = TaskConfig(
            description="t",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
            git=GitActions(),
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.config.settings.dev_fallback_runners", ["codex"]),
        ):
            result = orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        assert result == "codex output"
        mock_step.assert_called_once()
        args, kwargs = mock_step.call_args
        assert args == (1, "s", "success")
        assert kwargs["provider"] == "codex"

    def test_non_role_step_with_no_model_records_provider_only(self) -> None:
        """A non-role shell step: provider (runner kind) is known, model is
        genuinely unknown -> recorded as None, never invented."""
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry._definition_for.return_value = RunnerDefinition(
            name="shell", kind="shell", command="echo hi"
        )
        mock_runner = MagicMock()
        mock_runner.capture.return_value = "shell output"
        orch.registry.get_runner.return_value = mock_runner
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="shell", command="echo hi")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
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
            )

        mock_step.assert_called_once_with(1, "s", "success", provider="shell", model=None)

    def test_step_metadata_model_override_wins_over_runner_definition(self) -> None:
        """A step's own `metadata['model']` override (if set) takes priority
        over the resolved RunnerDefinition.model when threading into
        record_step — mirrors how the runners themselves resolve `model`."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "agent output"
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[
                TaskStep(
                    name="s",
                    runner="claude",
                    prompt_file="p.md",
                    metadata={"model": "override-model"},
                )
            ],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
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
            )

        mock_step.assert_called_once_with(
            1, "s", "success", provider="codex", model="override-model"
        )

    def test_role_with_no_effort_builds_runner_definition_with_effort_none(self) -> None:
        """Regression guard: the built-in `developer` role declares no
        `effort` -- the RunnerDefinition constructed for it at the role-based
        step-execution site (`resolve_runner` -> `RunnerDefinition(effort=...)`)
        must carry `effort=None`, never an invented value. This is the
        byte-identical-by-default contract for every existing role binding."""
        from hivepilot.models import GitActions, ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()

        captured_defs = []

        def capture_side_effect(runner_def, payload):
            captured_defs.append(runner_def)
            return "agent output"

        orch.registry.capture_definition.side_effect = capture_side_effect
        task = TaskConfig(
            description="t",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
            git=GitActions(),
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
            )

        assert len(captured_defs) == 1
        assert captured_defs[0].effort is None

    def test_role_with_effort_threads_it_into_runner_definition(self) -> None:
        """A role with an explicit `effort` (e.g. via roles.yaml/policy) must
        have that value carried onto the constructed RunnerDefinition at the
        role-based step-execution site — the plumbing `ClaudeRunner
        ._resolve_effort` later reads to set MAX_THINKING_TOKENS."""
        from hivepilot.models import GitActions, ProjectConfig, TaskConfig, TaskStep
        from hivepilot.roles import ROLES

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()

        captured_defs = []

        def capture_side_effect(runner_def, payload):
            captured_defs.append(runner_def)
            return "agent output"

        orch.registry.capture_definition.side_effect = capture_side_effect
        task = TaskConfig(
            description="t",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
            git=GitActions(),
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        original = ROLES["developer"]
        effortful = original.model_copy(update={"effort": "high"})
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch.dict(ROLES, {"developer": effortful}),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        assert len(captured_defs) == 1
        assert captured_defs[0].effort == "high"


# ---------------------------------------------------------------------------
# Phase 24b.2a — orchestrator threads captured usage (tokens/cost/actual
# model) from the runner into state_service.record_step.
# ---------------------------------------------------------------------------


class TestUsageThreading:
    def test_no_usage_captured_keeps_existing_call_signature(self) -> None:
        """When pop_last_usage() returns None (flag off / non-claude runner /
        no capture), record_step must be called EXACTLY as it was before this
        sprint — no input_tokens/output_tokens/cost_usd kwargs at all. This
        guarantees Phase 24b.1 callers/tests remain byte-compatible."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "agent output"
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.pop_last_usage", return_value=None),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        mock_step.assert_called_once_with(1, "s", "success", provider="codex", model="gpt-5.5")

    def test_captured_usage_threads_tokens_and_cost(self) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep
        from hivepilot.runners.base import UsageInfo

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "agent output"
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        usage = UsageInfo(input_tokens=100, output_tokens=50, cost_usd=0.02, model=None)
        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.pop_last_usage", return_value=usage),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        mock_step.assert_called_once_with(
            1,
            "s",
            "success",
            provider="codex",
            model="gpt-5.5",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.02,
        )

    def test_captured_usage_model_overrides_resolved_model(self) -> None:
        """A claude step's actual model (from the JSON envelope) closes the
        24b.1 gap where profile/default-model claude steps persisted None."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep
        from hivepilot.runners.base import UsageInfo

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "agent output"
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        usage = UsageInfo(
            input_tokens=1, output_tokens=2, cost_usd=0.001, model="claude-sonnet-4-6-actual"
        )
        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.pop_last_usage", return_value=usage),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        args, kwargs = mock_step.call_args
        assert kwargs["model"] == "claude-sonnet-4-6-actual"

    def test_failed_step_never_threads_usage(self) -> None:
        """A step that raised must never carry usage (capture() only stashes
        usage right before its successful return)."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = RuntimeError("boom")
        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md", allow_failure=True)],
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.pop_last_usage", return_value=None),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        mock_step.assert_called_once_with(
            1, "s", "failed", "boom", provider="codex", model="gpt-5.5"
        )


# ---------------------------------------------------------------------------
# Dual-model debate (item B) — DebateService wired & reachable
# ---------------------------------------------------------------------------


class TestDebate:
    def test_run_debate_one_position_per_model(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})

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
                task_name="ceo-intake",
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
                task_name="developer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=True,
            )
        mock_debate.assert_not_called()


# ---------------------------------------------------------------------------
# ${secret:NAME} reference resolution at the step-assembly site
# (Orchestrator._resolve_secrets)
# ---------------------------------------------------------------------------


class TestResolveSecretsReferences:
    def _orch(self):
        return _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))

    def test_direct_form_still_resolves(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig, TaskStep

        monkeypatch.setenv("HP_DIRECT_STORE", "direct-secret")
        orch = self._orch()
        step = TaskStep(
            name="s",
            runner="claude",
            secrets={"TOKEN": {"source": "env", "key": "HP_DIRECT_STORE"}},
        )
        project = ProjectConfig(path=Path("/tmp/p"))
        out = orch._resolve_secrets(step, project, None)
        assert out == {"TOKEN": "direct-secret"}

    def test_reference_reaches_payload_secrets(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig, TaskStep

        monkeypatch.setenv("HP_REF_STORE", "ref-secret-value")
        orch = self._orch()
        step = TaskStep(name="s", runner="claude")
        project = ProjectConfig(
            path=Path("/tmp/p"),
            env={"API_KEY": "${secret:openai}"},
            secrets={"openai": {"source": "env", "key": "HP_REF_STORE"}},
        )
        out = orch._resolve_secrets(step, project, None)
        assert out["API_KEY"] == "ref-secret-value"

    def test_pwd_style_token_left_untouched(self) -> None:
        from hivepilot.models import ProjectConfig, TaskStep

        orch = self._orch()
        step = TaskStep(name="s", runner="claude")
        # ${PWD} is NOT a secret ref: it must be ignored (no catalog lookup,
        # no error) and produce no payload.secrets entry.
        project = ProjectConfig(
            path=Path("/tmp/p"),
            env={"VOL": "${PWD}:/workspace"},
            secrets={"openai": {"source": "env", "key": "IRRELEVANT"}},
        )
        out = orch._resolve_secrets(step, project, None)
        assert "VOL" not in out

    def test_closed_mode_missing_reference_aborts(self) -> None:
        import pytest

        from hivepilot.models import ProjectConfig, TaskStep
        from hivepilot.services import policy_service
        from hivepilot.services.secret_refs import SecretReferenceError

        orch = self._orch()
        step = TaskStep(name="s", runner="claude")
        project = ProjectConfig(
            path=Path("/tmp/p"),
            env={"API_KEY": "${secret:absent}"},
            secrets={},
        )
        policy = policy_service.Policy(secrets_fail_mode="closed")
        with pytest.raises(SecretReferenceError) as exc:
            orch._resolve_secrets(step, project, policy)
        assert "absent" in str(exc.value)

    def test_no_project_falls_back_to_direct_only(self, monkeypatch) -> None:
        from hivepilot.models import TaskStep

        monkeypatch.setenv("HP_DIRECT_STORE", "d")
        orch = self._orch()
        step = TaskStep(
            name="s",
            runner="claude",
            secrets={"T": {"source": "env", "key": "HP_DIRECT_STORE"}},
        )
        # project=None (legacy call shape) still works.
        assert orch._resolve_secrets(step) == {"T": "d"}


# ---------------------------------------------------------------------------
# Resolved-secrets registry scoping (code review finding #2): the registry
# must be cleared once a run_task/run_pipeline call FULLY completes, but NOT
# prematurely when run_task is called as a NESTED stage of run_pipeline
# (pipeline-level sinks like write_stage_artifact/record_interaction execute
# AFTER the nested run_task call returns but BEFORE run_pipeline itself does).
# ---------------------------------------------------------------------------


class TestSecretRegistryRunScope:
    def _orch(self):
        return _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))

    def _clean(self):
        from hivepilot.services import config_provenance

        config_provenance.clear_secret_values()
        return config_provenance

    def test_balanced_enter_exit_clears_registry(self) -> None:
        cp = self._clean()
        orch = self._orch()
        cp.register_secret_value("scope-test-secret-one")
        orch._enter_run_scope()
        assert "scope-test-secret-one" in cp.registered_secret_values()
        orch._exit_run_scope()
        assert cp.registered_secret_values() == frozenset()

    def test_nested_scope_does_not_clear_until_outermost_exits(self) -> None:
        """Simulates run_pipeline (outer) calling run_task (inner) once per
        stage: entering twice then exiting once must NOT clear — the registry
        must still be available for the outer call's own post-stage sinks."""
        cp = self._clean()
        orch = self._orch()
        cp.register_secret_value("nested-scope-secret")

        orch._enter_run_scope()  # simulates run_pipeline entry
        orch._enter_run_scope()  # simulates nested run_task entry
        orch._exit_run_scope()  # simulates nested run_task returning
        assert "nested-scope-secret" in cp.registered_secret_values(), (
            "registry must survive the INNER call's exit — outer sinks still need it"
        )
        orch._exit_run_scope()  # simulates run_pipeline itself returning
        assert cp.registered_secret_values() == frozenset()

    def test_run_task_wrapper_clears_after_body_completes(self, monkeypatch) -> None:
        cp = self._clean()
        orch = self._orch()

        def _fake_body(**kwargs):
            cp.register_secret_value("body-registered-secret")
            return []

        monkeypatch.setattr(orch, "_run_task_body", _fake_body)
        orch.run_task(project_names=[], task_name="x", extra_prompt=None, auto_git=False)
        assert cp.registered_secret_values() == frozenset()

    def test_run_task_wrapper_clears_on_exception(self, monkeypatch) -> None:
        import pytest

        cp = self._clean()
        orch = self._orch()

        def _fake_body(**kwargs):
            cp.register_secret_value("body-registered-secret-2")
            raise RuntimeError("boom")

        monkeypatch.setattr(orch, "_run_task_body", _fake_body)
        with pytest.raises(RuntimeError):
            orch.run_task(project_names=[], task_name="x", extra_prompt=None, auto_git=False)
        assert cp.registered_secret_values() == frozenset()

    def test_run_pipeline_wrapper_clears_after_nested_run_task_call(self, monkeypatch) -> None:
        """The realistic nesting case: _run_pipeline_body internally calls the
        PUBLIC self.run_task(...) once (as it does per-stage). The registry
        must still hold the secret registered inside run_task's body when
        _run_pipeline_body's own post-stage code runs — proving nested calls
        don't clear prematurely — and only clears once run_pipeline itself
        returns."""
        cp = self._clean()
        orch = self._orch()
        seen_after_nested_call: set[str] = set()

        def _fake_pipeline_body(**kwargs):
            orch.run_task(project_names=[], task_name="x", extra_prompt=None, auto_git=False)
            # Pipeline-level sink work happens HERE, after run_task returns.
            seen_after_nested_call.update(cp.registered_secret_values())
            return []

        def _fake_task_body(**kwargs):
            cp.register_secret_value("pipeline-nested-secret")
            return []

        monkeypatch.setattr(orch, "_run_pipeline_body", _fake_pipeline_body)
        monkeypatch.setattr(orch, "_run_task_body", _fake_task_body)

        orch.run_pipeline(
            project_names=[], pipeline_name="test-pipe", extra_prompt=None, auto_git=False
        )
        assert "pipeline-nested-secret" in seen_after_nested_call, (
            "run_task's nested exit must NOT clear the registry before "
            "run_pipeline's own post-stage sinks run"
        )
        assert cp.registered_secret_values() == frozenset(), (
            "the registry must be cleared once the OUTERMOST run_pipeline call returns"
        )

    def test_run_debate_wrapper_clears_after_body_completes(self, monkeypatch) -> None:
        """Standalone run_debate (e.g. triggered repeatedly via ChatOps) must
        clear the registry on its own exit, same as run_task/run_pipeline."""
        cp = self._clean()
        orch = self._orch()

        def _fake_debate_body(**kwargs):
            cp.register_secret_value("debate-registered-secret")
            return {"adr": "stub"}

        monkeypatch.setattr(orch, "_run_debate_body", _fake_debate_body)
        orch.run_debate(project_name="p", role_name="cto", topic="t")
        assert cp.registered_secret_values() == frozenset()

    def test_run_debate_nested_inside_run_task_does_not_clear_prematurely(
        self, monkeypatch
    ) -> None:
        """A role-driven run_task that internally triggers run_debate (as
        _execute_task does for dual-model roles) must not have its own
        post-debate sinks see an emptied registry — the shared depth counter
        must treat run_debate as just another nested scope."""
        cp = self._clean()
        orch = self._orch()
        seen_after_nested_debate: set[str] = set()

        def _fake_task_body(**kwargs):
            orch.run_debate(project_name="p", role_name="cto", topic="t")
            seen_after_nested_debate.update(cp.registered_secret_values())
            return []

        def _fake_debate_body(**kwargs):
            cp.register_secret_value("task-nested-debate-secret")
            return {"adr": "stub"}

        monkeypatch.setattr(orch, "_run_task_body", _fake_task_body)
        monkeypatch.setattr(orch, "_run_debate_body", _fake_debate_body)

        orch.run_task(project_names=[], task_name="x", extra_prompt=None, auto_git=False)
        assert "task-nested-debate-secret" in seen_after_nested_debate, (
            "run_debate's nested exit must NOT clear the registry before "
            "the enclosing run_task's own sinks run"
        )
        assert cp.registered_secret_values() == frozenset(), (
            "the registry must be cleared once the OUTERMOST run_task call returns"
        )


# ---------------------------------------------------------------------------
# Phase 10c — RunResult.detail redaction choke point.
#
# A runner's captured stdout (`_execute_task`'s `task_result`) flows straight
# into `RunResult.detail`, which reaches sinks that do NOT redact themselves
# (cli.py's `typer.echo(result.detail)`, api_service's `/v1/run` response
# body, discord/slack/telegram `_format_results`) — unlike the DB/notification
# sinks (state_service, notification_service, artifact writers), which already
# redact via `config_provenance.redact_text`. These tests drive the REAL
# `Orchestrator.run_task`/`run_approved` path (the actual object cli/api/chat
# consume) with a fake runner whose output/exception embeds a registered
# secret value, and assert it never appears verbatim in the returned
# `RunResult.detail`.
# ---------------------------------------------------------------------------


class TestRunResultDetailRedaction:
    MARKER = "SUPERSECRET-RUNRESULT-MARKER-9f2c1a7b"

    def _orch_with_project_and_task(self, task):
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.projects.projects["proj"] = ProjectConfig(path=Path("/tmp/redact-proj"))
        orch.tasks.tasks["x"] = task
        return orch

    def _clean_registry(self):
        from hivepilot.services import config_provenance

        config_provenance.clear_secret_values()
        return config_provenance

    def test_run_task_redacts_registered_secret_from_success_detail(self, tmp_path) -> None:
        """A fake runner's `capture()` echoes a REGISTERED secret value in its
        stdout; the resulting `RunResult.detail` returned by the PUBLIC
        `run_task` must have it masked."""
        from hivepilot.models import TaskConfig, TaskStep
        from hivepilot.services.policy_service import Policy

        cp = self._clean_registry()
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="claude")],
            artifacts={"capture": []},
        )
        orch = self._orch_with_project_and_task(task)
        orch.registry = MagicMock()
        fake_runner = MagicMock()
        fake_runner.capture.return_value = f"leaked output {self.MARKER} end"
        orch.registry.get_runner.return_value = fake_runner

        cp.register_secret_value(self.MARKER)
        try:
            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch(
                    "hivepilot.orchestrator.policy_service.enforce_policy",
                    return_value=Policy(),
                ),
                patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
                patch("hivepilot.orchestrator.state_service.complete_run"),
                patch("hivepilot.orchestrator.state_service.record_step"),
                patch("hivepilot.orchestrator.notification_service.send_notification"),
                patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
                patch("hivepilot.orchestrator.create_run_directory", return_value=tmp_path),
            ):
                results = orch.run_task(
                    project_names=["proj"],
                    task_name="x",
                    extra_prompt=None,
                    auto_git=False,
                )
        finally:
            cp.clear_secret_values()

        assert len(results) == 1
        assert results[0].success is True
        assert self.MARKER not in (results[0].detail or ""), (
            "the secret leaked verbatim into the RunResult consumed by cli/api/chat"
        )
        assert cp.REDACTED in (results[0].detail or "")

    def test_run_task_redacts_registered_secret_from_failure_detail(self, tmp_path) -> None:
        """A step raises an exception whose message embeds a REGISTERED
        secret (e.g. a runner error echoing captured stdout) — the failure
        `RunResult.detail` from the PUBLIC `run_task` must have it masked."""
        from hivepilot.models import TaskConfig, TaskStep
        from hivepilot.services.policy_service import Policy

        cp = self._clean_registry()
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="claude")],
            artifacts={"capture": []},
        )
        orch = self._orch_with_project_and_task(task)
        orch.registry = MagicMock()
        fake_runner = MagicMock()
        fake_runner.capture.side_effect = RuntimeError(f"non-zero exit, stdout was: {self.MARKER}")
        orch.registry.get_runner.return_value = fake_runner

        cp.register_secret_value(self.MARKER)
        try:
            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch(
                    "hivepilot.orchestrator.policy_service.enforce_policy",
                    return_value=Policy(),
                ),
                patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
                patch("hivepilot.orchestrator.state_service.complete_run"),
                patch("hivepilot.orchestrator.state_service.record_step"),
                patch("hivepilot.orchestrator.notification_service.send_notification"),
                patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
                patch("hivepilot.orchestrator.create_run_directory", return_value=tmp_path),
            ):
                results = orch.run_task(
                    project_names=["proj"],
                    task_name="x",
                    extra_prompt=None,
                    auto_git=False,
                )
        finally:
            cp.clear_secret_values()

        assert len(results) == 1
        assert results[0].success is False
        assert self.MARKER not in (results[0].detail or ""), (
            "the secret leaked verbatim into the failure RunResult"
        )
        assert cp.REDACTED in (results[0].detail or "")

    def test_run_task_redacts_registered_secret_sent_to_notion_and_linear(self, tmp_path) -> None:
        """A step raises an exception whose message embeds a REGISTERED
        secret. The `_run_task_body` failure `except` block also forwards
        that message to `notion_service.on_run_complete` (Notion page PATCH)
        and `linear_service.on_run_failure` (Linear issue creation) — both
        external network sinks that do NOT self-redact. Both must receive
        the already-redacted text, never the raw exception string."""
        from hivepilot.models import TaskConfig, TaskStep
        from hivepilot.services.policy_service import Policy

        cp = self._clean_registry()
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="claude")],
            artifacts={"capture": []},
        )
        orch = self._orch_with_project_and_task(task)
        orch.registry = MagicMock()
        fake_runner = MagicMock()
        fake_runner.capture.side_effect = RuntimeError(f"non-zero exit, stdout was: {self.MARKER}")
        orch.registry.get_runner.return_value = fake_runner

        cp.register_secret_value(self.MARKER)
        try:
            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch(
                    "hivepilot.orchestrator.policy_service.enforce_policy",
                    return_value=Policy(),
                ),
                patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
                patch("hivepilot.orchestrator.state_service.complete_run"),
                patch("hivepilot.orchestrator.state_service.record_step"),
                patch("hivepilot.orchestrator.notification_service.send_notification"),
                patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
                patch("hivepilot.orchestrator.create_run_directory", return_value=tmp_path),
                patch(
                    "hivepilot.services.notion_service.on_run_start",
                    return_value="notion-page-123",
                ) as mock_on_run_start,
                patch("hivepilot.services.notion_service.on_run_complete") as mock_on_run_complete,
                patch("hivepilot.services.linear_service.on_run_failure") as mock_on_run_failure,
            ):
                results = orch.run_task(
                    project_names=["proj"],
                    task_name="x",
                    extra_prompt=None,
                    auto_git=False,
                )
        finally:
            cp.clear_secret_values()

        assert mock_on_run_start.called
        assert len(results) == 1
        assert results[0].success is False
        assert self.MARKER not in (results[0].detail or "")

        # Notion: `detail=` kwarg must be redacted, never the raw exception text.
        assert mock_on_run_complete.called, "on_run_complete was not called on failure"
        notion_kwargs = mock_on_run_complete.call_args.kwargs
        assert self.MARKER not in notion_kwargs.get("detail", ""), (
            "the secret leaked verbatim into the Notion page update"
        )
        assert cp.REDACTED in notion_kwargs.get("detail", "")

        # Linear: `error=` kwarg must be redacted, never the raw exception text.
        assert mock_on_run_failure.called, "on_run_failure was not called on failure"
        linear_kwargs = mock_on_run_failure.call_args.kwargs
        assert self.MARKER not in linear_kwargs.get("error", ""), (
            "the secret leaked verbatim into the Linear issue"
        )
        assert cp.REDACTED in linear_kwargs.get("error", "")

    def test_run_task_leaves_non_secret_detail_unchanged(self, tmp_path) -> None:
        """No secret registered -> `redact_text` is a no-op — the raw runner
        output passes through the choke point untouched (proves the fix
        doesn't over-redact / mangle ordinary output)."""
        from hivepilot.models import TaskConfig, TaskStep
        from hivepilot.services.policy_service import Policy

        self._clean_registry()
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="claude")],
            artifacts={"capture": []},
        )
        orch = self._orch_with_project_and_task(task)
        orch.registry = MagicMock()
        fake_runner = MagicMock()
        fake_runner.capture.return_value = "plain, unremarkable agent output"
        orch.registry.get_runner.return_value = fake_runner

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch(
                "hivepilot.orchestrator.policy_service.enforce_policy",
                return_value=Policy(),
            ),
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
            patch("hivepilot.orchestrator.create_run_directory", return_value=tmp_path),
        ):
            results = orch.run_task(
                project_names=["proj"],
                task_name="x",
                extra_prompt=None,
                auto_git=False,
            )

        assert len(results) == 1
        assert results[0].detail == "plain, unremarkable agent output"

    def test_run_approved_redacts_registered_secret_from_failure_detail(self) -> None:
        """`run_approved`'s post-approval `_execute_task` failure path is a
        separate RunResult construction site from `_run_task_body` — must
        independently redact a registered secret in `str(exc)`."""
        import json as _json

        from hivepilot.models import ProjectConfig, TaskConfig

        cp = self._clean_registry()
        task = TaskConfig(description="t", engine="native", steps=[])
        orch = self._orch_with_project_and_task(task)
        orch.projects.projects["proj"] = ProjectConfig(path=Path("/tmp/redact-proj"))

        approval_row = {
            "status": "pending",
            "project": "proj",
            "task": "x",
            "metadata": _json.dumps({}),
        }

        cp.register_secret_value(self.MARKER)
        try:
            with (
                patch(
                    "hivepilot.orchestrator.state_service.get_approval",
                    return_value=approval_row,
                ),
                patch("hivepilot.orchestrator.state_service.update_approval"),
                patch("hivepilot.orchestrator.state_service.complete_run"),
                patch("hivepilot.orchestrator.notification_service.send_notification"),
                patch(
                    "hivepilot.orchestrator.policy_service.get_policy",
                    return_value=None,
                ),
                patch.object(
                    orch,
                    "_execute_task",
                    side_effect=RuntimeError(f"agent echoed: {self.MARKER}"),
                ),
            ):
                result = orch.run_approved(run_id=1, approve=True, approver="tester")
        finally:
            cp.clear_secret_values()

        assert result.success is False
        assert self.MARKER not in (result.detail or ""), (
            "the secret leaked verbatim into run_approved's failure RunResult"
        )
        assert cp.REDACTED in (result.detail or "")


# ---------------------------------------------------------------------------
# Phase 21 Sprint 2 -- pipeline CVE gate (`policy.block_on_severity`)
#
# Mirrors the `require_approval` run-level pre-execution gate: BEFORE a run's
# steps are dispatched, `_run_task_body` may run `scan_service.
# scan_vulnerabilities` and block the run entirely (recorded as a failed run,
# `project` never added to `immediate_projects`, so no step/runner is ever
# invoked) if the scan finds anything at/above the configured severity, or if
# the scan itself fails (fail-closed).
# ---------------------------------------------------------------------------


class TestCveGate:
    def _orch_with_project_and_task(self, task):
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.projects.projects["proj"] = ProjectConfig(path=Path("/tmp/cve-gate-proj"))
        orch.tasks.tasks["x"] = task
        return orch

    def _task(self):
        from hivepilot.models import TaskConfig, TaskStep

        return TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="claude")],
            artifacts={"capture": []},
        )

    def _run_task(self, orch, *, policy, extra_patches=(), simulate=False, tmp_path=None):
        from contextlib import ExitStack

        fake_runner = MagicMock()
        fake_runner.capture.return_value = "step ran"
        orch.registry = MagicMock()
        orch.registry.get_runner.return_value = fake_runner

        with ExitStack() as stack:
            stack.enter_context(patch.object(orch, "_resolve_secrets", return_value={}))
            stack.enter_context(
                patch("hivepilot.orchestrator.policy_service.enforce_policy", return_value=policy)
            )
            stack.enter_context(
                patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1)
            )
            mock_complete_run = stack.enter_context(
                patch("hivepilot.orchestrator.state_service.complete_run")
            )
            stack.enter_context(patch("hivepilot.orchestrator.state_service.record_step"))
            mock_notify = stack.enter_context(
                patch("hivepilot.orchestrator.notification_service.send_notification")
            )
            stack.enter_context(patch("hivepilot.orchestrator.knowledge_service.append_feedback"))
            stack.enter_context(
                patch("hivepilot.orchestrator.create_run_directory", return_value=tmp_path)
            )
            for extra in extra_patches:
                stack.enter_context(extra)

            results = orch.run_task(
                project_names=["proj"],
                task_name="x",
                extra_prompt=None,
                auto_git=False,
                simulate=simulate,
            )
        return results, fake_runner, mock_complete_run, mock_notify

    def test_at_or_above_threshold_blocks_run_and_never_executes_step(self, tmp_path) -> None:
        from hivepilot.services.policy_service import Policy
        from hivepilot.services.scan_service import Finding, ScanResult

        policy = Policy(block_on_severity="critical")
        scan_result = ScanResult(
            tool="grype",
            total=1,
            by_severity={"critical": 1, "high": 0, "medium": 0},
            findings=[
                Finding(id="CVE-2099-0001", package="libfoo", version="1.0.0", severity="critical")
            ],
        )

        orch = self._orch_with_project_and_task(self._task())
        results, fake_runner, mock_complete_run, mock_notify = self._run_task(
            orch,
            policy=policy,
            tmp_path=tmp_path,
            extra_patches=(
                patch(
                    "hivepilot.orchestrator.scan_service.scan_vulnerabilities",
                    return_value=scan_result,
                ),
            ),
        )

        assert len(results) == 1
        assert results[0].success is False
        assert "Blocked by CVE gate" in (results[0].detail or "")
        assert "critical" in (results[0].detail or "")
        fake_runner.capture.assert_not_called()
        mock_complete_run.assert_called_once()
        assert mock_complete_run.call_args.args[1] == "failed"

    def test_below_threshold_proceeds_and_executes_step(self, tmp_path) -> None:
        from hivepilot.services.policy_service import Policy
        from hivepilot.services.scan_service import Finding, ScanResult

        policy = Policy(block_on_severity="critical")
        scan_result = ScanResult(
            tool="grype",
            total=1,
            by_severity={"critical": 0, "high": 0, "medium": 1},
            findings=[
                Finding(id="CVE-2099-0002", package="libbar", version="2.0.0", severity="medium")
            ],
        )

        orch = self._orch_with_project_and_task(self._task())
        results, fake_runner, mock_complete_run, mock_notify = self._run_task(
            orch,
            policy=policy,
            tmp_path=tmp_path,
            extra_patches=(
                patch(
                    "hivepilot.orchestrator.scan_service.scan_vulnerabilities",
                    return_value=scan_result,
                ),
            ),
        )

        assert len(results) == 1
        assert results[0].success is True
        fake_runner.capture.assert_called_once()

    def test_gate_unset_proceeds_without_calling_scan(self, tmp_path) -> None:
        """Default policy (block_on_severity=None): no overhead, no behaviour
        change -- scan_vulnerabilities must never be called."""
        from hivepilot.services.policy_service import Policy

        policy = Policy()  # block_on_severity defaults to None

        orch = self._orch_with_project_and_task(self._task())
        with patch("hivepilot.orchestrator.scan_service.scan_vulnerabilities") as mock_scan:
            results, fake_runner, _, _ = self._run_task(orch, policy=policy, tmp_path=tmp_path)

        mock_scan.assert_not_called()
        assert len(results) == 1
        assert results[0].success is True
        fake_runner.capture.assert_called_once()

    def test_simulate_bypasses_the_gate_entirely(self, tmp_path) -> None:
        """`--simulate` skips the CVE gate exactly like it skips
        `require_approval`: no scan, no block."""
        from hivepilot.services.policy_service import Policy

        policy = Policy(block_on_severity="critical")

        orch = self._orch_with_project_and_task(self._task())
        with patch("hivepilot.orchestrator.scan_service.scan_vulnerabilities") as mock_scan:
            results, fake_runner, _, _ = self._run_task(
                orch, policy=policy, tmp_path=tmp_path, simulate=True
            )

        mock_scan.assert_not_called()
        assert len(results) == 1
        assert results[0].success is True

    def test_scan_failure_blocks_fail_closed_and_never_executes_step(self, tmp_path) -> None:
        """A scanner that raises (missing binary, timeout, ...) must BLOCK
        the run -- a configured CVE gate must never fail open."""
        from hivepilot.services.policy_service import Policy

        policy = Policy(block_on_severity="critical")

        orch = self._orch_with_project_and_task(self._task())
        results, fake_runner, mock_complete_run, mock_notify = self._run_task(
            orch,
            policy=policy,
            tmp_path=tmp_path,
            extra_patches=(
                patch(
                    "hivepilot.orchestrator.scan_service.scan_vulnerabilities",
                    side_effect=RuntimeError("grype not found on PATH"),
                ),
            ),
        )

        assert len(results) == 1
        assert results[0].success is False
        detail = results[0].detail or ""
        assert "CVE gate configured but scan failed" in detail
        assert "RuntimeError" in detail
        # No secret/raw-scanner-output leak: never echo the exception message.
        assert "grype not found on PATH" not in detail
        fake_runner.capture.assert_not_called()

    def test_block_message_carries_only_severity_counts_no_raw_output(self, tmp_path) -> None:
        """Anti-leak: the block detail must be built from `by_severity`
        COUNTS only -- never raw scanner stdout or a specific package name
        that could embed lockfile/source material."""
        from hivepilot.services.policy_service import Policy
        from hivepilot.services.scan_service import Finding, ScanResult

        policy = Policy(block_on_severity="critical")
        scan_result = ScanResult(
            tool="grype",
            total=1,
            by_severity={"critical": 1, "high": 0, "medium": 0},
            findings=[
                Finding(
                    id="CVE-2099-9999",
                    package="LEAKY-PACKAGE-NAME-SHOULD-NOT-APPEAR",
                    version="1.0.0",
                    severity="critical",
                )
            ],
        )

        orch = self._orch_with_project_and_task(self._task())
        results, *_ = self._run_task(
            orch,
            policy=policy,
            tmp_path=tmp_path,
            extra_patches=(
                patch(
                    "hivepilot.orchestrator.scan_service.scan_vulnerabilities",
                    return_value=scan_result,
                ),
            ),
        )

        detail = results[0].detail or ""
        assert "LEAKY-PACKAGE-NAME-SHOULD-NOT-APPEAR" not in detail
        assert "CVE-2099-9999" not in detail
        assert "critical" in detail  # severity COUNTS dict is safe to include


# ---------------------------------------------------------------------------
# Phase 21 Sprint 3 -- CVE gate defense-in-depth on the `require_approval`
# resume path (`run_approved`).
#
# `require_approval` and `block_on_severity` are independent `if`/`elif`
# branches in `_run_task_body`: a project configured with BOTH only ever has
# the `require_approval` branch evaluated pre-run (records a pending
# approval, never reaches the CVE-gate `elif`), and `run_approved` dispatches
# an approved run straight to `_execute_task`. Without a gate check inside
# `run_approved` itself, an approver could approve straight past a critical
# CVE finding entirely ungated. These tests are the regression guard.
# ---------------------------------------------------------------------------


class TestCveGateRunApprovedResume:
    def _orch_with_project_and_task(self):
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.projects.projects["proj"] = ProjectConfig(path=Path("/tmp/cve-gate-approved-proj"))
        orch.tasks.tasks["x"] = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="claude")],
            artifacts={"capture": []},
        )
        return orch

    def _approval_row(self):
        import json as _json

        # No "kind" key -- this is the per-task `require_approval` pending
        # approval created by `_run_task_body`, NOT a step_checkpoint resume.
        return {
            "status": "pending",
            "project": "proj",
            "task": "x",
            "metadata": _json.dumps(
                {"task": "x", "project": "proj", "extra_prompt": None, "auto_git": False}
            ),
        }

    def test_approved_run_blocked_by_cve_gate_never_executes(self) -> None:
        """The regression guard for the HIGH finding: both gates configured,
        approval granted, but a critical finding must still block -- the
        approver cannot approve past the CVE gate."""
        from hivepilot.services.policy_service import Policy
        from hivepilot.services.scan_service import Finding, ScanResult

        policy = Policy(require_approval=True, block_on_severity="critical")
        scan_result = ScanResult(
            tool="grype",
            total=1,
            by_severity={"critical": 1, "high": 0, "medium": 0},
            findings=[
                Finding(id="CVE-2099-0003", package="libbaz", version="1.0.0", severity="critical")
            ],
        )

        orch = self._orch_with_project_and_task()

        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=self._approval_row(),
            ),
            patch("hivepilot.orchestrator.state_service.update_approval") as mock_update,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch("hivepilot.orchestrator.policy_service.get_policy", return_value=policy),
            patch.object(orch, "_execute_task") as mock_execute,
            patch(
                "hivepilot.orchestrator.scan_service.scan_vulnerabilities",
                return_value=scan_result,
            ),
        ):
            result = orch.run_approved(run_id=1, approve=True, approver="tester")

        assert result.success is False
        assert "Blocked by CVE gate" in (result.detail or "")
        mock_execute.assert_not_called()
        mock_complete.assert_called_once()
        assert mock_complete.call_args.args[1] == "failed"
        mock_update.assert_called_once()
        assert mock_update.call_args.args[1] == "approved"

    def test_approved_run_below_threshold_proceeds_to_execute(self) -> None:
        from hivepilot.services.policy_service import Policy
        from hivepilot.services.scan_service import Finding, ScanResult

        policy = Policy(require_approval=True, block_on_severity="critical")
        scan_result = ScanResult(
            tool="grype",
            total=1,
            by_severity={"critical": 0, "high": 0, "medium": 1},
            findings=[
                Finding(id="CVE-2099-0004", package="libqux", version="1.0.0", severity="medium")
            ],
        )

        orch = self._orch_with_project_and_task()

        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=self._approval_row(),
            ),
            patch("hivepilot.orchestrator.state_service.update_approval"),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch("hivepilot.orchestrator.policy_service.get_policy", return_value=policy),
            patch.object(orch, "_execute_task") as mock_execute,
            patch(
                "hivepilot.orchestrator.scan_service.scan_vulnerabilities",
                return_value=scan_result,
            ),
        ):
            result = orch.run_approved(run_id=1, approve=True, approver="tester")

        assert result.success is True
        mock_execute.assert_called_once()
        mock_complete.assert_called_once_with(1, "success")

    def test_reject_with_cve_gate_configured_not_double_handled(self) -> None:
        """Reject path is unaffected by the CVE gate check -- a denied run
        must not evaluate the gate at all (it's irrelevant once rejected)."""
        from hivepilot.services.policy_service import Policy

        policy = Policy(require_approval=True, block_on_severity="critical")
        orch = self._orch_with_project_and_task()

        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=self._approval_row(),
            ),
            patch("hivepilot.orchestrator.state_service.update_approval") as mock_update,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch(
                "hivepilot.orchestrator.policy_service.get_policy", return_value=policy
            ) as mock_get_policy,
            patch.object(orch, "_execute_task") as mock_execute,
            patch("hivepilot.orchestrator.scan_service.scan_vulnerabilities") as mock_scan,
        ):
            result = orch.run_approved(run_id=1, approve=False, approver="tester")

        assert result.success is False
        mock_execute.assert_not_called()
        mock_scan.assert_not_called()
        mock_get_policy.assert_not_called()
        mock_complete.assert_called_once()
        assert mock_complete.call_args.args[1] == "denied"
        mock_update.assert_called_once()
        assert mock_update.call_args.args[1] == "denied"


# ---------------------------------------------------------------------------
# Orchestrator.remediation_gate_present (Phase 20 D4 review MUST-FIX)
#
# Gated auto-remediation (`drift_schedule._attempt_remediation`) must refuse
# to dispatch a `remediate_task` that isn't provably approval-gated --
# `step_requires_approval` is fail-OPEN for a step whose runner has no
# `is_destructive` method (or whose resolved operation isn't apply/destroy).
# These tests exercise the REAL `remediation_gate_present`/`_find_gating_step`
# (no mocking of the gate-check itself) against real `TaskConfig`/`TaskStep`
# objects and the REAL `RunnerRegistry`, so a real `opentofu` apply step
# actually proves the preflight agrees with the real gate.
# ---------------------------------------------------------------------------


def _make_orchestrator_with_real_registry(task_name, task, project_name, project):
    """Like `_make_orchestrator_with_pipeline`, but deliberately does NOT
    stub `RunnerRegistry` -- the real registry (and therefore the real
    `is_destructive()` gate) is what `remediation_gate_present` must agree
    with."""
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    with (
        patch(
            "hivepilot.orchestrator.load_projects",
            return_value=MagicMock(projects={project_name: project}),
        ),
        patch(
            "hivepilot.orchestrator.load_tasks",
            return_value=MagicMock(tasks={task_name: task}, runners={}),
        ),
        patch("hivepilot.orchestrator.load_pipelines", return_value=PipelinesFile(pipelines={})),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    return orch


class TestRemediationGatePresent:
    def test_destructive_apply_step_is_gated(self, tmp_path: Path) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        task = TaskConfig(
            description="apply infra",
            steps=[TaskStep(name="apply", runner="opentofu", command="apply")],
        )
        project = ProjectConfig(path=tmp_path)
        orch = _make_orchestrator_with_real_registry("apply-infra", task, "proj-a", project)

        assert orch.remediation_gate_present("proj-a", "apply-infra") is True

    def test_non_destructive_plan_step_is_not_gated(self, tmp_path: Path) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        task = TaskConfig(
            description="plan only",
            steps=[TaskStep(name="plan", runner="opentofu", command="plan")],
        )
        project = ProjectConfig(path=tmp_path)
        orch = _make_orchestrator_with_real_registry("plan-only", task, "proj-a", project)

        assert orch.remediation_gate_present("proj-a", "plan-only") is False

    def test_unknown_task_fails_closed(self, tmp_path: Path) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig

        project = ProjectConfig(path=tmp_path)
        orch = _make_orchestrator_with_real_registry(
            "some-task", TaskConfig(description="x"), "proj-a", project
        )

        assert orch.remediation_gate_present("proj-a", "ghost-task") is False

    def test_unknown_project_fails_closed(self, tmp_path: Path) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        task = TaskConfig(
            description="apply infra",
            steps=[TaskStep(name="apply", runner="opentofu", command="apply")],
        )
        project = ProjectConfig(path=tmp_path)
        orch = _make_orchestrator_with_real_registry("apply-infra", task, "proj-a", project)

        assert orch.remediation_gate_present("ghost-project", "apply-infra") is False
