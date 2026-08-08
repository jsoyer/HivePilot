"""A refusal is a decision. It was recorded as the absence of one.

Every verdict on the box carries `decision = NULL` and `confidence = NULL` —
17 of 17 — while their own summaries say what the reviewers decided:

    adversarial review by reviewer, ciso, qa: reviewer: REQUEST_CHANGES
    adversarial review by qa: qa: REQUEST_CHANGES

The tokens were parsed. They were rendered into prose. Then the structured
columns were filled with nothing:

    verdict = (
        Verdict(decision="ACCEPT", confidence=1.0)
        if all_pass
        else Verdict(decision=None, confidence=None)
    )

Two consequences, and neither is a safety problem — `is_blocking` fails
closed on `None` exactly as it does on `REQUEST_CHANGES`:

- **The gate cannot tell a refusal from an unreadable answer.** Both arrive
  as "no decision", so an operator reading the row cannot know whether three
  reviewers rejected the change or three reviewers were unreachable.
- **`validate_lesson` has no confidence to rank on.** `max_verdict_confidence`
  is its only discriminating input, so 120 distilled lessons all scored at
  the admission floor even after #453 made the scale able to separate them.

So a non-pass outcome now records the token the reviewers actually produced,
and a confidence equal to the share of reviewers that could be read. Only a
review where NOTHING parsed stays `None` — because there, genuinely, nothing
was decided.

**The gate is untouched.** `_APPROVE_VERDICTS` is `{ACCEPT, ACCEPTED,
APPROVE, APPROVED}`; `REQUEST_CHANGES`, `BLOCKED` and `NEEDS_HUMAN` are not
in it, so `is_blocking` returns True for all three whatever their confidence.
This adds information to a row that already blocked; it never opens a door.
"""

from __future__ import annotations

import pytest

from hivepilot.orchestrator import _review_verdict_from_tokens
from hivepilot.services.git_service import is_blocking


class TestARefusalIsRecordedAsOne:
    def test_a_single_request_changes_is_named(self) -> None:
        verdict = _review_verdict_from_tokens(["REQUEST_CHANGES"])

        assert verdict.decision == "REQUEST_CHANGES"
        assert verdict.confidence == pytest.approx(1.0)

    def test_the_most_blocking_token_governs(self) -> None:
        """ "if more than one verdict applies, the most-blocking wins" — the
        same rule `_register_verdict` already applies across verdicts, now
        applied within one."""
        verdict = _review_verdict_from_tokens(["REQUEST_CHANGES", "BLOCKED", "PASS"])

        assert verdict.decision == "BLOCKED"

    def test_needs_human_outranks_request_changes(self) -> None:
        verdict = _review_verdict_from_tokens(["REQUEST_CHANGES", "NEEDS_HUMAN"])

        assert verdict.decision == "NEEDS_HUMAN"

    def test_confidence_is_the_share_of_reviewers_that_could_be_read(self) -> None:
        """Two of three answered; the third's silence is not evidence. A
        verdict derived from one voice out of three should not claim the
        certainty of three."""
        verdict = _review_verdict_from_tokens(["REQUEST_CHANGES", None, None])

        assert verdict.confidence == pytest.approx(1 / 3)


class TestUnanimousPassIsUnchanged:
    def test_all_pass_still_accepts_at_full_confidence(self) -> None:
        verdict = _review_verdict_from_tokens(["PASS", "PASS", "PASS"])

        assert verdict.decision == "ACCEPT"
        assert verdict.confidence == pytest.approx(1.0)

    def test_one_unreadable_reviewer_denies_the_accept(self) -> None:
        """`_parse_reviewer_verdict` returns None for a reviewer that cannot
        be read, and its docstring is explicit: callers must treat that as
        "this reviewer blocks", never an implicit pass."""
        verdict = _review_verdict_from_tokens(["PASS", "PASS", None])

        assert verdict.decision != "ACCEPT"


class TestNothingReadableStaysNone:
    def test_all_unparseable_is_no_decision(self) -> None:
        """Not a refusal — an absence. The distinction is the point of this
        change, so it has to survive it."""
        verdict = _review_verdict_from_tokens([None, None])

        assert verdict.decision is None
        assert verdict.confidence is None

    def test_no_reviewers_at_all_is_no_decision(self) -> None:
        verdict = _review_verdict_from_tokens([])

        assert verdict.decision is None
        assert verdict.confidence is None


class TestTheGateStaysClosed:
    """The failure mode to avoid: naming a decision that `is_blocking` then
    reads as approval. Checked against the real gate, at a threshold a
    confident verdict would clear."""

    @pytest.mark.parametrize(
        "tokens",
        [
            ["REQUEST_CHANGES"],
            ["BLOCKED"],
            ["NEEDS_HUMAN"],
            ["PASS", "REQUEST_CHANGES"],
            ["PASS", None],
            [None],
            [],
        ],
    )
    def test_every_non_unanimous_outcome_blocks(self, tokens: list[str | None]) -> None:
        assert is_blocking(_review_verdict_from_tokens(tokens), 0.7) is True

    def test_unanimous_pass_is_the_only_thing_that_proceeds(self) -> None:
        assert is_blocking(_review_verdict_from_tokens(["PASS", "PASS"]), 0.7) is False
