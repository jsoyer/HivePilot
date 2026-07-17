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
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import (
    GitActions,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    TaskConfig,
    TaskStep,
)
from hivepilot.runners.base import RunnerPayload
from hivepilot.services.state_service import RunStatus


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit, so `Orchestrator._is_git_repo`
    (and therefore `_use_worktree`) sees a genuine git repository — mirrors
    `tests/test_worktree_isolation.py`'s helper of the same name."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True
    )
    (tmp_path / "README.md").write_text("init")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True
    )
    return tmp_path


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


# ---------------------------------------------------------------------------
# Fail-closed guard: step-level approval gate + git-worktree isolation
# ---------------------------------------------------------------------------
#
# A step-level pause raises `StepApprovalPending`, which unwinds through the
# `with isolated_worktree(...)` block. That context manager's `finally`
# unconditionally runs `git worktree remove --force` (see
# `hivepilot.services.git_service.isolated_worktree`), so a mid-task pause in
# a worktree-isolated task would silently discard every prior step's file
# edits — a resume would then run against a FRESH worktree cut from unchanged
# HEAD. `_execute_task` must refuse this combination up front (before ever
# entering the worktree context) instead of losing work silently.


class TestWorktreeGateFailClosed:
    def test_destructive_step_refuses_when_task_uses_worktree_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "worktree_isolation", True)

        repo = _init_git_repo(tmp_path / "repo")
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task().model_copy(update={"git": GitActions(commit=True)})
        project = ProjectConfig(path=repo)

        with (
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval,
            patch("hivepilot.orchestrator.notification_service.send_approval_keyboard"),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            with pytest.raises(RuntimeError, match="git worktree isolation"):
                orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=True,
                    run_id=42,
                )

        # The refusal happens before the worktree (and therefore the step
        # loop) is ever entered — NEITHER step ran, not even the
        # non-destructive "prep" step, so nothing was silently lost.
        mock_wt.assert_not_called()
        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_not_called()
        # A plain failure, not a fake pause/approval state.
        mock_approval.assert_not_called()
        mock_complete.assert_not_called()

    def test_require_approval_flag_plus_git_push_also_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Covers both trigger types (`TaskStep.require_approval`, not just a
        runner-declared destructive op) and `task.git.push` (not just
        `.commit`) — either alone is enough to enable worktree isolation."""
        from hivepilot.config import settings as _settings
        from hivepilot.models import RunnerDefinition

        monkeypatch.setattr(_settings, "worktree_isolation", True)

        repo = _init_git_repo(tmp_path / "repo")
        orch = _make_orch()
        orch.registry = MagicMock()
        orch.registry._definition_for.return_value = RunnerDefinition(kind="shell")
        mock_runner = MagicMock()
        orch.registry.get_runner.return_value = mock_runner

        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="rm-prod", runner="shell", require_approval=True)],
            git=GitActions(push=True),
        )
        project = ProjectConfig(path=repo)

        with (
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            with pytest.raises(RuntimeError, match="git worktree isolation"):
                orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=True,
                    run_id=42,
                )

        mock_wt.assert_not_called()
        mock_runner.capture.assert_not_called()

    def test_resume_past_the_gate_is_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard only fires on the ORIGINAL run (`approved_step_index is
        None`). A task WITHOUT any remaining gating step (e.g. after its one
        destructive step has already been approved and is being resumed) must
        be allowed to proceed with worktree isolation."""
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "worktree_isolation", True)

        repo = _init_git_repo(tmp_path / "repo")
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task().model_copy(update={"git": GitActions(commit=True)})
        project = ProjectConfig(path=repo)

        with (
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.perform_git_actions"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            mock_wt.return_value.__enter__ = MagicMock(return_value=repo)
            mock_wt.return_value.__exit__ = MagicMock(return_value=False)

            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=True,
                run_id=42,
                resume_from_step=1,
                resume_outputs=["prep output"],
                approved_step_index=1,
            )

        mock_wt.assert_called_once()
        mock_prep.capture.assert_not_called()
        mock_apply.capture.assert_called_once()


# ---------------------------------------------------------------------------
# Fix: the destructive-approval gate check now runs BEFORE the `before_step`
# plugin hook, so a step that pauses has NOT fired `before_step` yet — it
# fires exactly once, when the step actually runs (on resume), instead of
# once (wastefully) before the pause and again on resume.
# ---------------------------------------------------------------------------


class TestBeforeStepHookOrdering:
    def test_before_step_fires_exactly_once_for_gated_step_across_pause_and_resume(
        self,
    ) -> None:
        from hivepilot.orchestrator import StepApprovalPending

        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        def _before_step_calls_for(step_name: str) -> list:
            return [
                c
                for c in orch.plugins.run_hook.call_args_list
                if c.args
                and c.args[0] == "before_step"
                and c.kwargs["payload"].step.name == step_name
            ]

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request"),
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

        # prep actually ran -> before_step fired for it exactly once.
        assert len(_before_step_calls_for("prep")) == 1
        # apply is gated and never ran -> before_step must NOT have fired for
        # it yet (this is the bug being fixed: previously it fired here too).
        assert len(_before_step_calls_for("apply")) == 0

        orch.plugins.run_hook.reset_mock()

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
                run_id=42,
                resume_from_step=1,
                resume_outputs=["prep output"],
                approved_step_index=1,
            )

        # On resume, apply actually runs -> before_step fires exactly once
        # for it (not zero, not twice).
        assert len(_before_step_calls_for("apply")) == 1
        # prep is never re-executed on resume -> before_step must not
        # re-fire for it either.
        assert len(_before_step_calls_for("prep")) == 0


# ---------------------------------------------------------------------------
# LOW: fail-OPEN on an unresolvable runner kind (distinct from fail-CLOSED
# when `is_destructive` itself raises, covered by
# `TestStepRequiresApproval.test_is_destructive_raising_fails_closed`).
# ---------------------------------------------------------------------------


class TestUnresolvableRunnerKindFailsOpen:
    def test_unknown_runner_kind_does_not_gate_a_normal_step(self) -> None:
        from hivepilot.models import RunnerDefinition

        orch = _make_orch()
        orch.registry = MagicMock()
        orch.registry._definition_for.return_value = RunnerDefinition(kind="totally-unknown-kind")
        mock_runner = MagicMock()
        mock_runner.capture.return_value = "output"
        orch.registry.get_runner.return_value = mock_runner

        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s", runner="mystery")],
        )
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
            )

        # An unresolvable runner kind can't be classified destructive, so it
        # must NOT gate — a normal step just runs.
        mock_approval.assert_not_called()
        mock_complete.assert_not_called()
        mock_runner.capture.assert_called_once()
        assert result == "output"


# ---------------------------------------------------------------------------
# LOW: a 3-step task with TWO destructive steps pauses twice, and no step
# ever runs more than once across the full pause -> approve -> resume ->
# pause -> approve -> resume cycle.
# ---------------------------------------------------------------------------


def _wire_three_step_registry(orch) -> tuple[MagicMock, MagicMock, MagicMock]:
    from hivepilot.models import RunnerDefinition

    defs = {
        "shell": RunnerDefinition(kind="shell"),
        "tf-a": RunnerDefinition(kind="terraform", command="apply"),
        "tf-b": RunnerDefinition(kind="terraform", command="apply"),
    }
    orch.registry = MagicMock()
    orch.registry._definition_for.side_effect = lambda name: defs[name]

    mock_prep = MagicMock()
    mock_prep.capture.return_value = "prep output"
    mock_apply1 = MagicMock()
    mock_apply1.capture.return_value = "apply1 output"
    mock_apply2 = MagicMock()
    mock_apply2.capture.return_value = "apply2 output"
    runners = {"shell": mock_prep, "tf-a": mock_apply1, "tf-b": mock_apply2}
    orch.registry.get_runner.side_effect = lambda name: runners[name]
    return mock_prep, mock_apply1, mock_apply2


def _three_step_task_two_destructive() -> TaskConfig:
    return TaskConfig(
        description="t",
        engine="native",
        steps=[
            TaskStep(name="prep", runner="shell"),
            TaskStep(name="apply1", runner="tf-a"),
            TaskStep(name="apply2", runner="tf-b"),
        ],
    )


class TestDoublePauseNoStepRunsTwice:
    def test_two_destructive_steps_each_pause_once_and_nothing_double_runs(self) -> None:
        from hivepilot.orchestrator import StepApprovalPending

        orch = _make_orch()
        mock_prep, mock_apply1, mock_apply2 = _wire_three_step_registry(orch)
        task = _three_step_task_two_destructive()
        project = ProjectConfig(path=Path("/tmp/p"))

        # --- Original run: pauses at step 1 ("apply1") ---
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval1,
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

        mock_prep.capture.assert_called_once()
        mock_apply1.capture.assert_not_called()
        mock_apply2.capture.assert_not_called()
        meta1 = mock_approval1.call_args.args[3]
        assert meta1["resume_from_step"] == 1
        assert meta1["step_name"] == "apply1"

        # --- First resume: approve step 1 -> runs "apply1", then pauses
        #     AGAIN at step 2 ("apply2") ---
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval2,
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
                    resume_from_step=1,
                    resume_outputs=list(meta1["resume_outputs"]),
                    approved_step_index=1,
                )

        mock_prep.capture.assert_called_once()  # still only once
        mock_apply1.capture.assert_called_once()  # exactly once, not re-run
        mock_apply2.capture.assert_not_called()
        meta2 = mock_approval2.call_args.args[3]
        assert meta2["resume_from_step"] == 2
        assert meta2["step_name"] == "apply2"

        # --- Second resume: approve step 2 -> runs "apply2", task completes ---
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
                resume_from_step=2,
                resume_outputs=list(meta2["resume_outputs"]),
                approved_step_index=2,
            )

        # No step ever ran more than once across the whole double-pause cycle.
        mock_prep.capture.assert_called_once()
        mock_apply1.capture.assert_called_once()
        mock_apply2.capture.assert_called_once()
        assert result == "prep output\napply1 output\napply2 output"


# ---------------------------------------------------------------------------
# Regression: STAGE-level skills must survive a destructive-step approval
# pause + `run_approved` resume (bug report).
# ---------------------------------------------------------------------------


class TestStageSkillsPersistAcrossApprovalResume:
    """A STAGE-level skill (`PipelineStage.skills`, threaded into
    `_execute_task` as `stage_skills`) must still be applied when a
    require_approval/destructive step pauses the task and is later resumed
    via `run_approved`. Root cause of the bug: the step-checkpoint
    `checkpoint_meta` never persisted `stage_skills`, and `run_approved`
    never re-threaded it back into the resumed `_execute_task` call, so it
    silently defaulted to `None` on resume -- even though the ORIGINAL
    (pre-pause) call had it. Step-level `step.skills` are unaffected (they
    live on the `TaskStep`, reconstructed fresh from `task.steps` on every
    call -- never lost)."""

    def test_stage_skills_persist_across_destructive_step_approval_resume(
        self, tmp_path: Path
    ) -> None:
        from hivepilot.models import RunnerDefinition
        from hivepilot.orchestrator import Orchestrator, StepApprovalPending
        from hivepilot.plugins import SkillSpec
        from hivepilot.runners.claude_runner import _SKILL_SCRATCH_DIR_KEY

        skill: SkillSpec = {
            "name": "demo",
            "description": "demo skill",
            "provider": "sample",
            "files": {"SKILL.md": "# Demo\nDo the thing."},
            "system_prompt": "Follow the demo skill.",
        }

        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("do the thing", encoding="utf-8")
        # A SINGLE step that requires approval regardless of runner kind
        # (`step.require_approval=True` -- see `step_requires_approval`) and
        # declares NO skill of its own -- only the enclosing stage's skill
        # (threaded in as `stage_skills`) should apply here.
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[
                TaskStep(
                    name="apply",
                    runner="claude",
                    prompt_file=str(prompt_file),
                    require_approval=True,
                    skills=None,
                )
            ],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
            patch(
                "hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})
            ),
            patch("hivepilot.orchestrator.load_pipelines", return_value=MagicMock(pipelines={})),
            patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
            patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        ):
            orch = Orchestrator()
        orch.plugins.get_skill.side_effect = lambda n: {"demo": skill}.get(n)  # type: ignore[attr-defined]
        orch.registry._definition_for.side_effect = lambda key: RunnerDefinition(  # type: ignore[attr-defined]
            name=key, kind="claude", command="claude"
        )
        orch.projects = MagicMock(projects={"p": project})
        orch.tasks = MagicMock(tasks={"x": task})

        # --- Original run: pauses at the (only, require_approval) step,
        #     carrying the stage-level skill in via `stage_skills`. ---
        with (
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.record_approval_request") as mock_approval,
            patch("hivepilot.orchestrator.notification_service.send_approval_keyboard"),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            mock_settings.worktree_isolation = False
            mock_settings.stage_cache_enabled = False
            mock_settings.dev_batch_size = 0
            with pytest.raises(StepApprovalPending):
                orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=False,
                    run_id=42,
                    stage_skills=["demo"],
                )

        mock_approval.assert_called_once()
        checkpoint_meta = mock_approval.call_args.args[3]
        assert checkpoint_meta["kind"] == "step_checkpoint"

        # --- Resume via `run_approved`, exactly as production does: the
        #     ONLY state carried across the pause is `checkpoint_meta`,
        #     persisted to the approval row and reloaded via
        #     `state_service.get_approval` -- we round-trip it through JSON
        #     for realism, exactly like the real approval row does. ---
        approval_row = {
            "status": "pending",
            "project": "p",
            "task": "x",
            "metadata": json.dumps(checkpoint_meta),
        }
        seen: dict[str, object] = {}

        def _recorder(_self: object, runner_key: str, payload: object) -> str:
            seen["payload"] = payload
            return "ok"

        with (
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval_row),
            patch("hivepilot.orchestrator.state_service.update_approval"),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch("hivepilot.orchestrator.policy_service.get_policy", return_value=None),
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch.object(Orchestrator, "_capture_or_execute", _recorder),
        ):
            mock_settings.worktree_isolation = False
            mock_settings.stage_cache_enabled = False
            mock_settings.dev_batch_size = 0
            result = orch.run_approved(run_id=42, approve=True, approver="me")

        assert result.success is True, result.detail
        assert "payload" in seen, "the resumed step never reached the runner"
        scratch = seen["payload"].metadata.get(_SKILL_SCRATCH_DIR_KEY)  # type: ignore[attr-defined]
        assert scratch is not None, (
            "stage-level skill was dropped across the destructive-step "
            "approval resume: the scratch dir was never materialised for "
            "the resumed step's payload"
        )
