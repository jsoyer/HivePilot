"""The reviewer needs a perimeter, not more permissions.

Measured across three dispatches on one 3.8 KB diff:

    23 bare commands, 1 099 726 cache tokens   $1.54
    15 bare commands, 1 003 249 cache tokens   $1.17

`ls -la /`, `find / -maxdepth 6`, `cat …` — the reviewer walks the whole
filesystem. It never runs `gh pr diff`, even now that it may.

Three permission hypotheses were tested and none was the cause:
`permission_mode` is not required, the `rtk` prefix mattered only for `rtk`
and `gh`, and threading role options did not bound anything — Claude Code
auto-allows recognised read-only commands (`find`, `ls`, `grep`, `cat`) and
gates only unknown binaries. Proven: `find /tmp …` runs under an allowlist
naming only `echo`, while `rtk ls /tmp` under the same allowlist is refused.

So the allowlist OPENS (rtk, gh); it does not CLOSE. The lever is the
instruction: the prompt asks for reasons to reject and never says what to
read, or how far to look. An agent given a diff and no perimeter explores.

This is the same lesson as the `status:` line — the instruction that
commissions the answer is the one that governs it.
"""

from __future__ import annotations

from hivepilot.orchestrator import _build_review_challenge_prompt


class TestThePromptStatesThePerimeter:
    def test_it_says_the_diff_is_complete(self) -> None:
        """The reviewer cannot know the subject is the whole change unless
        told. Absent that, exploring is the reasonable thing to do."""
        prompt = _build_review_challenge_prompt("diff --git a/x b/x").lower()

        assert "complete" in prompt or "entire" in prompt

    def test_it_makes_exploration_a_last_resort(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/x b/x").lower()

        assert "only if" in prompt or "do not explore" in prompt

    def test_it_requires_exploration_to_be_declared(self) -> None:
        """Silent exploration is what made a 3.8 KB review cost $1.54.

        Requiring it to be stated keeps the option open — a reviewer that
        genuinely needs context must still be able to get it — while making
        the cost visible instead of mysterious.
        """
        prompt = _build_review_challenge_prompt("diff --git a/x b/x").lower()

        assert "say so" in prompt or "state" in prompt

    def test_it_points_at_the_pr_rather_than_the_filesystem(self) -> None:
        """`gh pr diff` is granted and unused because nothing names it."""
        prompt = _build_review_challenge_prompt("diff --git a/x b/x")

        assert "gh pr diff" in prompt


class TestNothingEarlierIsLost:
    """Bounding the reviewer must not soften it or break the parser."""

    def test_it_still_demands_adversarial_scrutiny(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/x b/x").lower()

        assert "not to approve it by default" in prompt
        assert "reject" in prompt

    def test_it_still_names_the_status_line_and_every_token(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/x b/x")

        assert "status:" in prompt.lower()
        for token in ("PASS", "REQUEST_CHANGES", "BLOCKED", "NEEDS_HUMAN"):
            assert token in prompt

    def test_the_subject_still_reaches_the_reviewer(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/secret b/secret")

        assert "diff --git a/secret b/secret" in prompt
