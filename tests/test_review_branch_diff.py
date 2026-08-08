"""The adversarial review gate has never seen a diff.

On the box: **17 verdicts, zero carrying a decision, zero carrying a
confidence.** The recorded summaries say why —

    blocked: empty subject — no diff was produced for reviewers

So the gate that is supposed to weigh reviewer / CISO / QA judgement before
promoting a PR has, every single time, blocked on nothing rather than
judged something. Downstream, `validate_lesson`'s only discriminating input
is `max_verdict_confidence`, which is therefore always None — which is why
100 distilled lessons all scored identically even after #453 made the scale
capable of separating them.

The cause is a directory, not a policy. `review_target: "github_pr"` called
`gh pr diff` with no arguments, which resolves *the current branch's PR*,
inside `_exec_project.path`. That path is the stage's own **isolated
worktree**, created from the base branch — so its HEAD is `staging`, which
has no PR, and the implementation's commits live on `hivepilot/<project>`
in the shared `.git` rather than in this worktree at all. `gh` answered
truthfully: `no pull requests found for branch "staging"`.

`git diff <base>...<branch>` is what a pull request actually shows: the
merge-base to the branch tip. It reads the shared refs, so the worktree's
own HEAD is irrelevant; it needs no network; and it works identically
whether or not a PR has been opened yet — which matters, because the review
runs BEFORE its stage's git actions, so on a first pass there is no PR to
ask about.

It is not the working-tree diff the old comment (rightly) refused: that
would let a review pass having read something other than what is about to
be promoted. `base...branch` IS what is about to be promoted.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services.review_probe import ReviewProbeError, fetch_branch_diff


def _run(**kw):
    return patch("hivepilot.services.review_probe.subprocess.run", **kw)


class TestItAsksForTheRightThing:
    def test_it_diffs_the_merge_base_to_the_branch_tip(self) -> None:
        with _run(return_value=MagicMock(returncode=0, stdout="diff --git a b\n", stderr="")) as m:
            fetch_branch_diff("/repo", base="staging", branch="hivepilot/noxys")

        assert m.call_args.args[0] == [
            "git",
            "diff",
            "staging...hivepilot/noxys",
        ], "three dots: a PR shows merge-base..tip, not the two-dot range"

    def test_it_runs_in_the_repo_it_was_given(self) -> None:
        with _run(return_value=MagicMock(returncode=0, stdout="d", stderr="")) as m:
            fetch_branch_diff("/repo", base="main", branch="b")

        assert m.call_args.kwargs["cwd"] == "/repo"

    def test_it_returns_the_diff(self) -> None:
        with _run(return_value=MagicMock(returncode=0, stdout="THE DIFF", stderr="")):
            assert fetch_branch_diff("/repo", base="main", branch="b") == "THE DIFF"


class TestItFailsClosed:
    def test_an_unknown_ref_raises(self) -> None:
        """Fail-closed is the point: `_run_review` turns a raised probe into
        an empty subject and blocks. Returning "" here would look like a
        clean review of nothing."""
        with _run(
            return_value=MagicMock(
                returncode=128, stdout="", stderr="fatal: bad revision 'staging...nope'"
            )
        ):
            with pytest.raises(ReviewProbeError, match="bad revision"):
                fetch_branch_diff("/repo", base="staging", branch="nope")

    def test_an_empty_diff_raises_rather_than_returning_nothing(self) -> None:
        """A branch with no changes against its base has nothing to review,
        and that must not read as an approved review."""
        with _run(return_value=MagicMock(returncode=0, stdout="   \n", stderr="")):
            with pytest.raises(ReviewProbeError, match="empty"):
                fetch_branch_diff("/repo", base="main", branch="b")

    def test_git_not_runnable_raises(self) -> None:
        with _run(side_effect=OSError("boom")):
            with pytest.raises(ReviewProbeError):
                fetch_branch_diff("/repo", base="main", branch="b")

    def test_a_timeout_raises(self) -> None:
        with _run(side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)):
            with pytest.raises(ReviewProbeError):
                fetch_branch_diff("/repo", base="main", branch="b")
