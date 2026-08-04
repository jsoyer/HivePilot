"""Reviewers must be able to answer in a form the parser can read.

Measured in production on 2026-08-04, the first day verdicts were recorded
at all: **4 of 6 reviewer responses came back UNPARSEABLE.**

    #1 reviewer: UNPARSEABLE
    #2 qa:       REQUEST_CHANGES
    #3 qa:       UNPARSEABLE
    #4 qa:       UNPARSEABLE
    #5 qa:       REQUEST_CHANGES
    #6 qa:       UNPARSEABLE

Every unparseable answer counts as non-PASS and blocks. So the gate was
blocking not because reviewers found problems, but because nobody could read
what they said — two thirds of paid-for review work discarded.

Two causes, both fixed here:

1. **The governing instruction never asked for the format.** The reviewers get
   their role prompt (which does mandate `status:`) *plus* the adversarial
   challenge prompt as `extra_prompt`. The challenge prompt asked for prose —
   "list every concrete reason this change should be REJECTED" — and never
   mentioned `status:` or the tokens. The more immediate, more specific
   instruction won, as it should.

2. **The parser rejected ordinary markdown.** It tolerated a `-` bullet but
   not the bold an agent writing a report naturally reaches for. Same class of
   bug as the agent-report parser two days earlier, same fix.

Widening the parser must never loosen its fail-closed contract: decoration is
tolerated, ambiguity is still refused.
"""

from __future__ import annotations

import pytest

from hivepilot.orchestrator import _build_review_challenge_prompt, _parse_reviewer_verdict


class TestTheChallengePromptStatesTheFormat:
    """The instruction that governs the answer has to name the format.

    Relying on the role prompt alone is what produced 4 unparseable answers
    out of 6: a general format rule loses to a specific task instruction.
    """

    def test_it_names_the_status_line(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/x b/x")

        assert "status:" in prompt.lower()

    def test_it_names_every_accepted_token(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/x b/x")

        for token in ("PASS", "REQUEST_CHANGES", "BLOCKED", "NEEDS_HUMAN"):
            assert token in prompt, f"{token} is accepted but never offered to the reviewer"

    def test_it_still_asks_for_adversarial_scrutiny(self) -> None:
        """Adding a format must not turn the reviewer into a rubber stamp."""
        prompt = _build_review_challenge_prompt("diff --git a/x b/x").lower()

        assert "reject" in prompt
        assert "not to approve it by default" in prompt

    def test_the_subject_still_reaches_the_reviewer(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/secret b/secret")

        assert "diff --git a/secret b/secret" in prompt


class TestTheParserToleratesMarkdown:
    """An agent writing a report reaches for bold. That is not ambiguity."""

    @pytest.mark.parametrize(
        "line",
        [
            "status: PASS",
            "- status: PASS",
            "**status:** PASS",
            "**status**: PASS",
            "- **status:** PASS",
            "## status: PASS",
            "  status : pass",
        ],
    )
    def test_decoration_is_read_through(self, line: str) -> None:
        assert _parse_reviewer_verdict(f"Some analysis.\n\n{line}\n") == "PASS"

    def test_the_other_tokens_parse_too(self) -> None:
        assert _parse_reviewer_verdict("**status:** REQUEST_CHANGES") == "REQUEST_CHANGES"
        assert _parse_reviewer_verdict("- **status:** BLOCKED") == "BLOCKED"
        assert _parse_reviewer_verdict("## status: NEEDS_HUMAN") == "NEEDS_HUMAN"


class TestFailClosedIsUnchanged:
    """Widening the parser must not weaken it. Each case still blocks."""

    def test_no_status_line_still_blocks(self) -> None:
        assert _parse_reviewer_verdict("This change looks fine to me.") is None

    def test_an_unrecognised_token_still_blocks(self) -> None:
        assert _parse_reviewer_verdict("status: LGTM") is None

    def test_contradictory_verdicts_still_block(self) -> None:
        """Two different verdicts is ambiguity, and ambiguity never guesses."""
        assert _parse_reviewer_verdict("status: PASS\nstatus: BLOCKED") is None

    def test_empty_output_still_blocks(self) -> None:
        assert _parse_reviewer_verdict("") is None
        assert _parse_reviewer_verdict("   \n\n  ") is None

    def test_the_word_pass_in_prose_is_not_a_verdict(self) -> None:
        """Only an explicit `status:` line counts.

        A reviewer writing "I would pass on this approach" means the opposite
        of approval; reading it as PASS would be the worst possible failure
        for a gate whose whole job is to withhold approval.
        """
        assert _parse_reviewer_verdict("I would pass on this approach entirely.") is None

    def test_the_same_verdict_repeated_is_not_ambiguous(self) -> None:
        """Restating one's own conclusion is not contradiction."""
        assert _parse_reviewer_verdict("status: BLOCKED\n\n...\n\nstatus: BLOCKED") == "BLOCKED"
