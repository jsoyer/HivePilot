"""The prompt must not call a truncated diff "complete".

`_build_review_challenge_prompt` cuts the subject at 4 000 characters. The
perimeter added in #419 then tells the reviewer:

    The diff above is the COMPLETE change under review. Review it as given.
    Do not explore the repository or the filesystem unless ...

On a diff that fits, both statements are true and the reviewer answers from
the prompt — measured at 9 259 cache tokens and $0.17 against 1 003 249 and
$1.17. On a diff that does not fit, the same sentence is a **lie that also
forbids the reviewer from compensating for it**: real PRs in this repo run
to 72 KB and 2.6 MB, where 4 000 characters is 5% and 0.15% of the change.

Before #419 the reviewer explored, which was expensive but partly covered
the gap. #419 removed the compensation without removing the cause, which
made the gate cheaper on small diffs and blind on large ones.

So the prompt states which case it is in, and says how much was withheld.
A reviewer told it is seeing a fragment can ask for the rest; one told the
fragment is everything cannot.
"""

from __future__ import annotations

from hivepilot.orchestrator import _REVIEW_SUBJECT_LIMIT, _build_review_challenge_prompt


class TestAWholeDiffIsCalledWhole:
    def test_it_claims_completeness_only_when_true(self) -> None:
        prompt = _build_review_challenge_prompt("diff --git a/x b/x\n+one line")

        assert "COMPLETE change" in prompt
        assert "truncated" not in prompt.lower()

    def test_the_perimeter_still_applies(self) -> None:
        """The cheap path must survive: a whole diff still means no roaming."""
        prompt = _build_review_challenge_prompt("diff --git a/x b/x").lower()

        assert "do not explore" in prompt


class TestATruncatedDiffSaysSo:
    @staticmethod
    def _big() -> str:
        """Derived from the limit, never a hardcoded size.

        The first version hardcoded 28 KB, which stopped being "big" the
        moment the cap was raised to 200 000 — the tests went green on a
        subject that was no longer truncated at all, testing nothing.
        """
        return "diff --git a/x b/x\n" + ("+padding line\n" * (_REVIEW_SUBJECT_LIMIT // 10))

    def test_it_never_calls_a_fragment_complete(self) -> None:
        prompt = _build_review_challenge_prompt(self._big())

        assert "COMPLETE change" not in prompt

    def test_it_says_the_diff_was_truncated(self) -> None:
        prompt = _build_review_challenge_prompt(self._big()).lower()

        assert "truncated" in prompt

    def test_it_quantifies_what_is_missing(self) -> None:
        """ "Truncated" alone leaves the reviewer guessing whether it saw
        almost everything or almost nothing — 4 000 characters is 95% of one
        real PR here and 0.15% of another."""
        big = self._big()
        prompt = _build_review_challenge_prompt(big)

        assert str(len(big)) in prompt

    def test_it_lets_the_reviewer_go_and_get_the_rest(self) -> None:
        """The perimeter must lift exactly when the premise for it fails.

        Forbidding exploration while withholding 95% of the change is worse
        than the roaming it was meant to stop.
        """
        prompt = _build_review_challenge_prompt(self._big()).lower()

        assert "gh pr diff" in prompt
        assert "do not explore" not in prompt


class TestUnchangedContract:
    def test_the_status_line_survives_both_paths(self) -> None:
        for subject in ("small diff", "x" * (_REVIEW_SUBJECT_LIMIT + 1000)):
            prompt = _build_review_challenge_prompt(subject)
            assert "status:" in prompt.lower()
            for token in ("PASS", "REQUEST_CHANGES", "BLOCKED", "NEEDS_HUMAN"):
                assert token in prompt

    def test_adversarial_framing_survives_both_paths(self) -> None:
        for subject in ("small diff", "x" * (_REVIEW_SUBJECT_LIMIT + 1000)):
            prompt = _build_review_challenge_prompt(subject).lower()
            assert "not to approve it by default" in prompt
