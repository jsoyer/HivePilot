"""#454 aimed at the right comparison and read the wrong refs.

`base...branch` is what a pull request shows — that part was right. But it
resolved *local* refs, and on the box the local branch pointer does not hold
the work:

    local  staging          e46ca4a30477
    local  hivepilot/noxys  e46ca4a30477   <- reset to the base
    origin staging          e46ca4a30477
    origin hivepilot/noxys  7bf85fa1eba2   <- the actual commits

    git diff staging...hivepilot/noxys                 ->      1 byte
    git diff origin/staging...origin/hivepilot/noxys   -> 23 247 bytes

Every stage runs in a fresh isolated worktree created from the base, and
`perform_git_actions` calls `checkout_branch(project.path, branch)` there. A
stage that commits nothing therefore leaves the LOCAL branch pointer sitting
at the worktree's HEAD — the base. The commits are on the remote, pushed by
the implementation stage.

So run 412 completed all fifteen stages and its release gate still recorded

    blocked: empty subject — no diff was produced for reviewers

for the same reason as before, one layer down. The remote-tracking refs are
what the PR is actually made of, and they are what a gate deciding whether to
promote that PR must read.

Local refs stay as a fallback for the case the remote-tracking ref does not
exist — a branch never pushed — rather than failing outright on it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services.review_probe import ReviewProbeError, fetch_branch_diff


def _runs(*results):
    """Successive `subprocess.run` results, in call order."""
    return patch("hivepilot.services.review_probe.subprocess.run", side_effect=list(results))


def _ok(stdout: str):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "fatal: bad revision"):
    return MagicMock(returncode=128, stdout="", stderr=stderr)


class TestItPrefersTheRemoteRefs:
    def test_it_asks_for_origin_refs_first(self) -> None:
        with _runs(_ok("THE DIFF")) as m:
            fetch_branch_diff("/repo", base="staging", branch="hivepilot/noxys")

        assert m.call_args_list[0].args[0] == [
            "git",
            "diff",
            "origin/staging...origin/hivepilot/noxys",
        ]

    def test_the_remote_diff_is_what_is_returned(self) -> None:
        with _runs(_ok("REMOTE DIFF")):
            assert fetch_branch_diff("/repo", base="staging", branch="b") == "REMOTE DIFF"

    def test_a_locally_reset_branch_no_longer_reads_as_empty(self) -> None:
        """The exact production shape: the local pointer sits at the base, so
        the local range is empty while the remote range carries the work."""
        with _runs(_ok("x" * 23_247)) as m:
            diff = fetch_branch_diff("/repo", base="staging", branch="hivepilot/noxys")

        assert len(diff) == 23_247
        assert m.call_count == 1, "the remote range answered; no need to fall back"


class TestItFallsBackToLocalRefs:
    def test_an_unpushed_branch_falls_back(self) -> None:
        """A branch that was never pushed has no remote-tracking ref. That is
        not a reason to refuse to review it."""
        with _runs(
            _fail("fatal: bad revision 'origin/staging...origin/b'"), _ok("LOCAL DIFF")
        ) as m:
            diff = fetch_branch_diff("/repo", base="staging", branch="b")

        assert diff == "LOCAL DIFF"
        assert m.call_args_list[1].args[0] == ["git", "diff", "staging...b"]

    def test_an_empty_remote_range_also_falls_back(self) -> None:
        """`origin/base...origin/branch` can be legitimately empty while the
        local branch holds uncommitted-to-remote work — the mirror of the
        production case, and equally worth reviewing."""
        with _runs(_ok("   \n"), _ok("LOCAL DIFF")):
            assert fetch_branch_diff("/repo", base="staging", branch="b") == "LOCAL DIFF"

    def test_both_empty_still_raises(self) -> None:
        """Fail-closed survives: nothing anywhere means nothing to review,
        and that must not read as a review that found nothing wrong."""
        with _runs(_ok(""), _ok("  ")):
            with pytest.raises(ReviewProbeError, match="empty diff"):
                fetch_branch_diff("/repo", base="staging", branch="b")

    def test_both_failing_raises_naming_the_local_attempt(self) -> None:
        with _runs(_fail(), _fail("fatal: bad revision 'staging...b'")):
            with pytest.raises(ReviewProbeError, match="staging...b"):
                fetch_branch_diff("/repo", base="staging", branch="b")
