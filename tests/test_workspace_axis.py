"""Isolation stops being a side effect of `auto_git`.

`_use_worktree` was derived, never declared:

    settings.worktree_isolation AND not simulate AND auto_git
    AND (task.git.commit OR task.git.push) AND is_git_repo

So a task ran in a throwaway worktree because it happened to COMMIT. Two
consequences, both real and both invisible in any config file:

  * a task that does not commit could NOT be isolated, however much you wanted
    it to be;
  * step-level approval had to be refused inside a worktree by a runtime
    exception rather than a readable constraint — nothing declared the two
    incompatible, because nothing declared the workspace at all.

`workspace:` names the axis. `"derive"` is the default and reproduces the
expression exactly, so every existing config is unchanged; `"shared"` and
`"worktree"` say it outright.

The half that matters most here is `TestDeriveIsUnchanged`. An axis nobody asked
for is not worth one altered dispatch.
"""

from __future__ import annotations

import pytest

from hivepilot.models import GitActions, TaskConfig


def _decide(
    *,
    declared: str,
    setting: bool = True,
    simulate: bool = False,
    auto_git: bool = True,
    commits: bool = True,
    is_repo: bool = True,
) -> bool:
    """The orchestrator's decision, in isolation.

    Mirrors the branch under test rather than importing it: the real one is
    buried mid-`_execute_task_body` behind a full run. `TestTheRealBranch`
    below pins that this copy still matches the source.
    """
    possible = not simulate and is_repo
    if declared == "shared":
        return False
    if declared == "worktree":
        return possible
    return setting and possible and auto_git and commits


class TestDeriveIsUnchanged:
    """`derive` must reproduce the old expression exactly. Every config in
    existence relies on it, having never heard of this field."""

    @pytest.mark.parametrize(
        ("setting", "simulate", "auto_git", "commits", "is_repo", "expected"),
        [
            (True, False, True, True, True, True),  # the ordinary isolated case
            (False, False, True, True, True, False),  # setting off
            (True, True, True, True, True, False),  # simulate
            (True, False, False, True, True, False),  # no auto_git
            (True, False, True, False, True, False),  # commits nothing
            (True, False, True, True, False, False),  # not a git repo
        ],
    )
    def test_each_conjunct_still_decides(
        self, setting, simulate, auto_git, commits, is_repo, expected
    ):
        assert (
            _decide(
                declared="derive",
                setting=setting,
                simulate=simulate,
                auto_git=auto_git,
                commits=commits,
                is_repo=is_repo,
            )
            is expected
        )

    def test_it_is_the_default_on_the_model(self):
        assert TaskConfig(description="x").workspace == "derive"


class TestDeclaringIt:
    def test_shared_refuses_a_worktree_even_when_the_derivation_would_want_one(self):
        """The case that could not be expressed: a task that commits, on a
        host with isolation on, deliberately running in the real tree."""
        assert _decide(declared="shared") is False

    def test_worktree_isolates_a_task_that_commits_nothing(self):
        """The other case that could not be expressed. Isolation was reachable
        only through `git.commit`/`git.push`, so a read-only or exploratory
        task was stuck in the shared tree."""
        assert _decide(declared="worktree", commits=False, auto_git=False) is True

    def test_worktree_ignores_the_global_setting(self):
        """An explicit declaration is a decision, not a preference to be
        overruled by a host-wide default."""
        assert _decide(declared="worktree", setting=False) is True


class TestPossibilityIsNotPreference:
    """`simulate` and "is this a git repo" stay OUTSIDE the choice. They say
    whether a worktree is POSSIBLE, not whether one is wanted — an explicit
    `workspace: worktree` must not conjure one where it cannot exist."""

    def test_an_explicit_worktree_on_a_non_repo_does_not_pretend(self):
        assert _decide(declared="worktree", is_repo=False) is False

    def test_an_explicit_worktree_under_simulate_does_not_pretend(self):
        """A simulated run must not create a real worktree on disk."""
        assert _decide(declared="worktree", simulate=True) is False

    def test_shared_is_still_shared_on_a_non_repo(self):
        assert _decide(declared="shared", is_repo=False) is False


class TestWhatTheAxisUnlocks:
    """Step-level approval is refused inside a worktree — a mid-task pause
    would discard it. Before this field, a task that COMMITTED was forced into
    a worktree, so it could never have a step gate. `workspace: shared` is now
    the way to have both."""

    def test_a_committing_task_can_now_opt_out_and_keep_its_step_gate(self):
        forced_before = _decide(declared="derive", commits=True)
        assert forced_before is True, "the old derivation forced a worktree here"

        assert _decide(declared="shared", commits=True) is False

    def test_the_guard_still_fires_for_a_real_worktree(self):
        """The refusal itself is unchanged — this axis gives an alternative,
        it does not weaken the constraint."""
        import inspect

        from hivepilot.orchestrator import Orchestrator

        src = inspect.getsource(Orchestrator._execute_task_body)

        assert "Step-level approval" in src
        assert "_use_worktree and approved_step_index is None" in src


class TestTheRealBranch:
    """`_decide` above is a copy, and a copy can drift from what ships. These
    read the source so the copy cannot quietly stop describing it."""

    def test_the_orchestrator_reads_the_declared_workspace(self):
        import inspect

        from hivepilot.orchestrator import Orchestrator

        src = inspect.getsource(Orchestrator._execute_task_body)

        assert 'getattr(task, "workspace", "derive")' in src

    def test_all_three_kinds_are_branched_on(self):
        import inspect

        from hivepilot.orchestrator import Orchestrator

        src = inspect.getsource(Orchestrator._execute_task_body)

        assert '_declared_workspace == "shared"' in src
        assert '_declared_workspace == "worktree"' in src

    def test_the_derive_branch_keeps_its_short_circuit(self):
        """`_is_git_repo` is a filesystem probe and must stay LAST, behind the
        cheap conjuncts.

        The first version hoisted it into a shared `_worktree_possible`
        computed up front — tidier, and wrong: it ran for every task, which
        `test_role_without_implementation_output_never_checks_git` and
        `test_mode_api_on_non_agent_runner_fails_before_subprocess` both caught
        by asserting it is never called. Saying it twice is the price of not
        touching a disk nobody asked about."""
        import inspect

        from hivepilot.orchestrator import Orchestrator

        src = inspect.getsource(Orchestrator._execute_task_body)
        derive = src[src.index("_declared_workspace = getattr") :]
        derive = derive[: derive.index("_keep_failed")] if "_keep_failed" in derive else derive

        probe = "self._is_git_repo(project.path)"
        assert derive.count(probe) == 2, "one per branch that needs it, never hoisted"
        # in the derive branch it comes after every cheap test
        tail = derive[derive.index("settings.worktree_isolation") :]
        assert tail.index(probe) > tail.index("task.git.commit")


class TestTheModelRefusesNonsense:
    def test_an_unknown_kind_is_rejected_at_config_load(self):
        """Fail at load, not at dispatch. A typo in `workspace:` must not
        silently fall through to `derive` — that is the shape of every silent
        defect this codebase keeps finding."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskConfig(description="x", workspace="wortkree")

    def test_container_is_not_a_workspace_yet(self):
        """`kind: container` is still a RUNNER. Accepting it here would
        advertise an axis member that nothing implements."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskConfig(description="x", workspace="container")

    def test_it_travels_with_git_actions_untouched(self):
        """The field is additive: declaring a workspace says nothing about
        whether the task commits."""
        task = TaskConfig(
            description="x", workspace="shared", git=GitActions(commit=True, push=True)
        )

        assert task.workspace == "shared"
        assert task.git.commit is True
