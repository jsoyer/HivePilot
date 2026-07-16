"""Phase 17a-B (Part B-core) — step-level approval gate for destructive
runner operations.

Extends the existing per-task (`policy.require_approval`) and per-stage
(`PipelineStage.pause_before`) approval mechanisms one level finer: a single
step within a task can require human approval before it runs, either because
the step opts in explicitly (`TaskStep.require_approval`) or because the
resolved runner declares its current operation destructive
(`runner.is_destructive(payload)`).

Covers:
- `step_requires_approval` — the pure decision helper.
- The orchestrator's step-level pause (mirrors `PipelineStage.pause_before`
  one level finer): a destructive step pauses `_execute_task` mid-task,
  records an approval request, marks the run PAUSED, and never executes the
  gated step (or anything after it).
- `run_approved` resuming a step checkpoint: prior steps are NOT re-executed
  (no double side-effects), their accumulated output is restored, and a
  reject aborts without ever running the gated step.
- `simulate=True` bypasses the gate entirely (no real destructive op runs
  under --simulate, consistent with how simulate bypasses every other
  approval mechanism in this codebase).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import PipelineConfig, PipelineStage, ProjectConfig, TaskConfig, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.services.state_service import RunStatus

# ---------------------------------------------------------------------------
# step_requires_approval — pure decision helper
# ---------------------------------------------------------------------------


def _payload() -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=Path("/tmp/proj")),
        task_name="t",
        step=TaskStep(name="s", runner="terraform"),
        metadata={},
        secrets={},
    )


class TestStepRequiresApproval:
    def test_destructive_runner_gates_even_without_step_flag(self) -> None:
        from hivepilot.orchestrator import step_requires_approval

        runner = MagicMock()
        runner.is_destructive.return_value = True
        step = TaskStep(name="apply", runner="terraform")

        assert step_requires_approval(runner, step, _payload()) is True

    def test_non_destructive_runner_with_step_flag_true_gates(self) -> None:
        from hivepilot.orchestrator import step_requires_approval

        runner = MagicMock()
        runner.is_destructive.return_value = False
        step = TaskStep(name="s", runner="shell", require_approval=True)

        assert step_requires_approval(runner, step, _payload()) is True

    def test_non_destructive_runner_with_flag_false_does_not_gate(self) -> None:
        from hivepilot.orchestrator import step_requires_approval

        runner = MagicMock()
        runner.is_destructive.return_value = False
        step = TaskStep(name="s", runner="shell", require_approval=False)

        assert step_requires_approval(runner, step, _payload()) is False

    def test_runner_without_is_destructive_method_does_not_gate(self) -> None:
        """Default absent = False (structural contract, like `capture`)."""
        from hivepilot.orchestrator import step_requires_approval

        runner = object()  # no is_destructive attribute at all
        step = TaskStep(name="s", runner="claude")

        assert step_requires_approval(runner, step, _payload()) is False

    def test_none_runner_does_not_gate_unless_flag_set(self) -> None:
        from hivepilot.orchestrator import step_requires_approval

        step = TaskStep(name="s", runner="claude")
        assert step_requires_approval(None, step, _payload()) is False

    def test_is_destructive_raising_fails_closed(self) -> None:
        """An error classifying the operation must never silently let a
        potentially-destructive step through ungated."""
        from hivepilot.orchestrator import step_requires_approval

        runner = MagicMock()
        runner.is_destructive.side_effect = RuntimeError("boom")
        step = TaskStep(name="apply", runner="terraform")

        assert step_requires_approval(runner, step, _payload()) is True


# ---------------------------------------------------------------------------
# Orchestrator-level: destructive step pauses _execute_task mid-task
# ---------------------------------------------------------------------------


def _make_pipeline_by_name(*names: str) -> PipelineConfig:
    return PipelineConfig(description="test", stages=[PipelineStage(name=n, task=n) for n in names])


def _make_orch():
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"test-pipe": _make_pipeline_by_name("x")})
    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        return Orchestrator()


def _two_step_task() -> TaskConfig:
    """A non-destructive 'prep' step followed by a destructive 'apply' step —
    the shape the PRD calls out (plan/prep -> gated apply, one task)."""
    return TaskConfig(
        description="t",
        engine="native",
        steps=[
            TaskStep(name="prep", runner="shell"),
            TaskStep(name="apply", runner="terraform"),
        ],
    )


def _wire_registry(orch) -> tuple[MagicMock, MagicMock]:
    """Wire orch.registry so 'shell' and 'terraform' resolve to distinct
    fake runner instances (independently call-counted) — real
    RunnerDefinitions are used (not bare MagicMocks) so the step-level gate's
    own runner resolution (`_resolve_runner_for_destructive_check`, which
    bypasses `self.registry` and resolves the REAL runner class by kind) sees
    a genuine `terraform` kind and finds the real `is_destructive`."""
    from hivepilot.models import RunnerDefinition

    defs = {
        "shell": RunnerDefinition(kind="shell"),
        "terraform": RunnerDefinition(kind="terraform", command="apply"),
    }
    orch.registry = MagicMock()
    orch.registry._definition_for.side_effect = lambda name: defs[name]

    mock_prep = MagicMock()
    mock_prep.capture.return_value = "prep output"
    mock_apply = MagicMock()
    mock_apply.capture.return_value = "apply output"
    runners = {"shell": mock_prep, "terraform": mock_apply}
    orch.registry.get_runner.side_effect = lambda name: runners[name]
    return mock_prep, mock_apply


class TestDestructiveStepPauses:
    def test_destructive_step_pauses_and_is_not_executed(self) -> None:
        from hivepilot.orchestrator import StepApprovalPending

        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval,
            patch("hivepilot.orchestrator.notification_service.send_approval_keyboard") as mock_kb,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            with pytest.raises(StepApprovalPending):
                orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=False,
                    run_id=42,
                )

        # prep ran exactly once; the destructive apply step was NEVER executed
        mock_prep.capture.assert_called_once()
        mock_apply.capture.assert_not_called()

        mock_approval.assert_called_once()
        args = mock_approval.call_args.args
        assert args[0] == 42
        assert args[1] == "p"
        assert args[2] == "x"
        meta = args[3]
        assert meta["kind"] == "step_checkpoint"
        assert meta["resume_from_step"] == 1
        assert meta["step_name"] == "apply"
        assert meta["resume_outputs"] == ["prep output"]

        mock_kb.assert_called_once()
        mock_complete.assert_called_once_with(42, RunStatus.PAUSED.value)

    def test_non_destructive_step_with_require_approval_flag_also_gates(self) -> None:
        """A plain shell step flagged `require_approval=True` gates exactly
        like a runner-declared destructive one."""
        from hivepilot.models import RunnerDefinition
        from hivepilot.orchestrator import StepApprovalPending

        orch = _make_orch()
        orch.registry = MagicMock()
        orch.registry._definition_for.return_value = RunnerDefinition(kind="shell")
        mock_runner = MagicMock()
        orch.registry.get_runner.return_value = mock_runner

        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="rm-prod", runner="shell", require_approval=True)],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval,
            patch("hivepilot.orchestrator.notification_service.send_approval_keyboard"),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            with pytest.raises(StepApprovalPending):
                orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=False,
                    run_id=42,
                )

        mock_runner.capture.assert_not_called()
        meta = mock_approval.call_args.args[3]
        assert meta["step_name"] == "rm-prod"
        assert meta["resume_from_step"] == 0

    def test_simulate_bypasses_the_gate_entirely(self) -> None:
        """Under --simulate, no real destructive op runs and the gate never
        fires — matches how simulate bypasses every other approval path."""
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            result = orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=42,
                simulate=True,
            )

        mock_approval.assert_not_called()
        mock_complete.assert_not_called()
        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_not_called()
        assert result == "[simulated shell]\n[simulated terraform]"


# ---------------------------------------------------------------------------
# Resume mechanics: prior steps not re-run, context restored
# ---------------------------------------------------------------------------


class TestExecuteTaskResume:
    def test_resume_skips_prior_steps_and_restores_their_output(self) -> None:
        """Direct `_execute_task` resume (the mechanism `run_approved`
        drives): `resume_from_step`/`approved_step_index` skip the prep step
        entirely (no double side-effect) and `resume_outputs` restores its
        output into the final task_result alongside the newly-run step."""
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            result = orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=42,
                resume_from_step=1,
                resume_outputs=["prep output"],
                approved_step_index=1,
            )

        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_called_once()
        # prior step's output is preserved, not lost, across the resume
        assert result == "prep output\napply output"


# ---------------------------------------------------------------------------
# run_approved — the resume/reject plumbing for a step checkpoint
# ---------------------------------------------------------------------------


def _step_checkpoint_approval(*, resume_from_step: int = 1) -> dict:
    return {
        "status": "pending",
        "project": "proj",
        "task": "x",
        "metadata": json.dumps(
            {
                "kind": "step_checkpoint",
                "task": "x",
                "project": "proj",
                "extra_prompt": None,
                "auto_git": False,
                "dry_run": True,
                "resume_from_step": resume_from_step,
                "step_name": "apply",
                "resume_outputs": ["prep output"],
            }
        ),
    }


class TestRunApprovedStepCheckpoint:
    def _orch_with_task(self):
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))
        orch.projects = MagicMock(projects={"proj": project})
        orch.tasks = MagicMock(tasks={"x": task})
        return orch, mock_prep, mock_apply

    def test_approve_runs_from_paused_step_without_rerunning_prior(self) -> None:
        orch, mock_prep, mock_apply = self._orch_with_task()
        approval = _step_checkpoint_approval()

        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch("hivepilot.orchestrator.state_service.update_approval") as mock_update,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch("hivepilot.orchestrator.policy_service.get_policy", return_value=None),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            result = orch.run_approved(run_id=42, approve=True, approver="me")

        assert result.success is True
        mock_update.assert_called_once()
        assert mock_update.call_args.args[1] == "approved"
        # the prep step (already-approved-and-run before the pause) is NEVER
        # re-executed — only the gated apply step runs, exactly once
        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_called_once()
        mock_complete.assert_called_once_with(42, "success")

    def test_reject_aborts_without_running_the_gated_step(self) -> None:
        orch, mock_prep, mock_apply = self._orch_with_task()
        approval = _step_checkpoint_approval()

        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch("hivepilot.orchestrator.state_service.update_approval") as mock_update,
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch("hivepilot.orchestrator.notification_service.send_notification"),
        ):
            result = orch.run_approved(run_id=42, approve=False, approver="me")

        assert result.success is False
        assert mock_update.call_args.args[1] == "denied"
        mock_complete.assert_called_once()
        assert mock_complete.call_args.args[1] == "denied"
        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_not_called()

    def test_reentrant_double_approve_does_not_rerun(self) -> None:
        """A second `run_approved` call against an approval row that's no
        longer 'pending' (already approved by the first call) must refuse,
        not double-run the step."""
        orch, mock_prep, mock_apply = self._orch_with_task()
        already_approved = dict(_step_checkpoint_approval())
        already_approved["status"] = "approved"

        with patch(
            "hivepilot.orchestrator.state_service.get_approval", return_value=already_approved
        ):
            with pytest.raises(ValueError, match="not pending"):
                orch.run_approved(run_id=42, approve=True, approver="me")

        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_not_called()
