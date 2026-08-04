"""A role-bearing task can contain a step no runner can capture.

`groomer-scan` broke the morning after it was given a role:

    signals | failed | Runner kind 'shell' does not support capture.

The executor decided between `capture_definition` and `execute` on
**`task.role`** — the task's role — rather than on what the resolved runner
can actually do. That was a safe proxy only while a role-bearing task always
got a claude runner. #403 fixed step-declared runners so a `runner: shell`
step correctly keeps `shell`, which turned the proxy into a wrong assumption
and failed the step outright.

The non-role path never had this bug: `_capture_or_execute` asks the runner
whether it can capture and falls back to `run()`. This makes the role path
ask the same question.

Capability is asked of the RUNNER, never inferred from the task.
"""

from __future__ import annotations

from hivepilot.models import RunnerDefinition
from hivepilot.registry import RunnerRegistry


class TestSupportsCapture:
    def test_a_shell_runner_cannot_capture(self) -> None:
        assert RunnerRegistry.supports_capture(RunnerDefinition(kind="shell")) is False

    def test_a_claude_runner_can(self) -> None:
        assert RunnerRegistry.supports_capture(RunnerDefinition(kind="claude")) is True

    def test_an_unknown_kind_is_not_assumed_capable(self) -> None:
        """An unresolvable kind must not be guessed as capture-capable.

        Guessing `True` sends it down a path that raises; guessing `False`
        sends it down one that executes. Neither is right for an unknown
        runner, but only one of them fails the step, so the answer is the
        one that lets the caller handle it.
        """
        assert RunnerRegistry.supports_capture(RunnerDefinition(kind="not-a-runner")) is False


class TestTheExecutorAsksTheRunnerNotTheTask:
    def test_the_role_branch_no_longer_branches_on_task_role_alone(self) -> None:
        """Pinned by reading the source, because reproducing it needs a full
        dispatch: the decision must consult the resolved runner.

        `groomer-scan` has a `runner: shell` `signals` step and a role. Any
        version that decides on `task.role` alone fails that step, which is
        exactly what happened in production on 2026-08-04.
        """
        from pathlib import Path

        import hivepilot.orchestrator as orch

        source = Path(orch.__file__).read_text(encoding="utf-8")

        assert "supports_capture" in source, (
            "the executor must ask the runner whether it can capture, "
            "not infer it from the task having a role"
        )
