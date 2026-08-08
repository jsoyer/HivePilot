"""Eleven stages of work, $25.78, killed at the second-to-last stage.

Run 425 completed intake, spec, synthesis, CTO, design, implementation,
review, security, QA and design review — then died on the documentation
stage:

    git checkout -B hivepilot/noxys
    fatal: 'hivepilot/noxys' is already used by worktree at '/root/noxys'

A branch can only be checked out in one worktree at a time. The main clone
was parked on `hivepilot/noxys` (left there by an earlier stage's
`perform_git_actions`), and a later stage running in its own isolated
worktree tried to claim the same branch. Git refused, correctly.

The cost is not the git error. It is that the run never reached **PR
approval**, which is the stage carrying the adversarial review gate — so the
verdict that #456 taught to record a decision was never produced, and the
question that run existed to answer went unanswered.

Two HivePilot features interacting, each right alone: `perform_git_actions`
checks the branch out in `project.path`, and worktree isolation runs stages
somewhere else. They only collide when both touch the same branch.

Retrying with `--ignore-other-worktrees` is safe **here specifically**
because stages are serialised: the parked clone is idle, not writing. It is
scoped to this one stderr signature — never a blanket force — and logged, so
a genuine concurrent claim would still be visible rather than silently
overridden.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from git import GitCommandError

from hivepilot.services.git_service import checkout_branch

_CLAIMED = (
    "Cmd('git') failed due to: exit code(128)\n"
    "  cmdline: git checkout -B hivepilot/noxys\n"
    "  stderr: 'fatal: 'hivepilot/noxys' is already used by worktree at '/root/noxys''"
)


def _repo_raising(*errors):
    """A repo whose `git.checkout` raises each error in turn, then succeeds."""
    repo = MagicMock()
    repo.git.checkout.side_effect = list(errors) + [None]
    return repo


class TestAClaimedBranchIsRetried:
    def test_it_retries_ignoring_other_worktrees(self, tmp_path: Path) -> None:
        repo = _repo_raising(GitCommandError("checkout", 128, stderr=_CLAIMED))
        with patch("hivepilot.services.git_service.ensure_repo", return_value=repo):
            checkout_branch(tmp_path, "hivepilot/noxys")

        second = repo.git.checkout.call_args_list[1]
        assert "--ignore-other-worktrees" in second.args

    def test_the_first_attempt_stays_plain(self, tmp_path: Path) -> None:
        """The flag is a fallback, not the normal path — a clean checkout
        must not quietly start overriding other worktrees."""
        repo = MagicMock()
        repo.git.checkout.return_value = None
        with patch("hivepilot.services.git_service.ensure_repo", return_value=repo):
            checkout_branch(tmp_path, "b")

        assert repo.git.checkout.call_count == 1
        assert "--ignore-other-worktrees" not in repo.git.checkout.call_args.args

    def test_a_failing_retry_still_raises(self, tmp_path: Path) -> None:
        repo = MagicMock()
        repo.git.checkout.side_effect = GitCommandError("checkout", 128, stderr=_CLAIMED)
        with patch("hivepilot.services.git_service.ensure_repo", return_value=repo):
            with pytest.raises(RuntimeError, match="Failed to checkout"):
                checkout_branch(tmp_path, "hivepilot/noxys")


class TestEveryOtherFailureStillRaises:
    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: not a git repository",
            "error: Your local changes would be overwritten",
            "fatal: invalid reference: nope",
        ],
    )
    def test_an_unrelated_git_error_is_not_retried(self, tmp_path: Path, stderr: str) -> None:
        """Scoped to one signature. A blanket retry with
        `--ignore-other-worktrees` would turn every checkout failure into a
        forced one."""
        repo = MagicMock()
        repo.git.checkout.side_effect = GitCommandError("checkout", 1, stderr=stderr)
        with patch("hivepilot.services.git_service.ensure_repo", return_value=repo):
            with pytest.raises(RuntimeError, match="Failed to checkout"):
                checkout_branch(tmp_path, "b")

        assert repo.git.checkout.call_count == 1, "an unrelated failure must not be retried"
