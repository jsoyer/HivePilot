"""The review gate could not review a large PR at all.

Exercised with `hivepilot review pr 428 --repo noxys-eu/noxys --execute` —
the probe built precisely so this gate can be tested without waiting for a
pipeline:

    reviewer: CALL_FAILED · ciso: CALL_FAILED · qa: CALL_FAILED
    Total: $0.0000

    claude_runner.prompt_too_large_for_argv
      prompt_bytes: 148390   limit_bytes: 131072

`_REVIEW_SUBJECT_LIMIT` was 200 000 — comfortably above the 131 072 bytes
Linux allows in a single argv element, which is how the runner passes a
prompt. So the existing truncation could never prevent the failure it looks
like it should. Any diff over roughly 123 KB made every reviewer fail, at
zero cost and with no verdict.

This was the floor under three layers that each looked like the problem:
`[Errno 7]` naming the binary (fixed in #447), `CALL_FAILED` with a NULL
cost (#445), and a verdict of `decision=NULL` indistinguishable from a
refusal (#456). Each fix peeled one off; this is what was underneath.

Two changes, and the second is why this is not merely a smaller truncation:

- the inline cap drops below the argv limit with room for the runner's own
  scaffolding, so the failure becomes structurally impossible;
- the FULL subject is written to a file in the workspace and named in the
  perimeter clause, so a reviewer can read all of it — possible only since
  the roles gained `Read(./**)` today.

Fail-soft on purpose: a reviewer that ignores the file still holds a large,
honestly-declared excerpt. A pure file-reference design would hand an
ignoring reviewer nothing at all.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.orchestrator import _REVIEW_SUBJECT_LIMIT, _build_review_challenge_prompt

# Linux `MAX_ARG_STRLEN`. The runner passes the assembled prompt as one argv
# element, so this is the wall the whole prompt must clear.
_MAX_ARG_STRLEN = 131_072


class TestTheCapClearsTheArgvLimit:
    def test_the_limit_leaves_room_for_the_runners_scaffolding(self) -> None:
        """Observed on the box: a 140 896-byte subject became a 148 390-byte
        prompt. The runner adds a role prompt, knowledge context and its own
        framing on top of whatever this builds."""
        assert _REVIEW_SUBJECT_LIMIT < _MAX_ARG_STRLEN
        assert _MAX_ARG_STRLEN - _REVIEW_SUBJECT_LIMIT >= 20_000, (
            "too little headroom for the runner's own additions"
        )

    def test_a_huge_diff_still_produces_a_prompt_under_the_wall(self) -> None:
        prompt = _build_review_challenge_prompt("x" * 500_000)

        assert len(prompt.encode()) < _MAX_ARG_STRLEN


class TestTheFullSubjectIsReachable:
    def test_the_perimeter_names_the_file_when_one_is_given(self) -> None:
        prompt = _build_review_challenge_prompt(
            "y" * 500_000, subject_path=".hivepilot-review-subject.diff"
        )

        assert ".hivepilot-review-subject.diff" in prompt

    def test_it_says_to_read_it(self) -> None:
        """Naming a path a reviewer does not know to open is decoration."""
        prompt = _build_review_challenge_prompt("y" * 500_000, subject_path="subject.diff")

        assert "Read" in prompt

    def test_a_short_diff_names_no_file(self) -> None:
        """Nothing was cut, so there is nothing to go and fetch — an
        instruction to read a file would invite a pointless tool call."""
        prompt = _build_review_challenge_prompt("small diff", subject_path="subject.diff")

        assert "subject.diff" not in prompt


class TestTheTruncationStaysHonest:
    def test_a_cut_diff_still_declares_itself_truncated(self) -> None:
        prompt = _build_review_challenge_prompt("z" * 500_000)

        assert "TRUNCATED" in prompt

    def test_a_cut_diff_never_claims_to_be_complete(self) -> None:
        """Telling a reviewer that a fragment is the whole change, while
        forbidding it from looking further, is worse than the roaming the
        perimeter exists to stop."""
        prompt = _build_review_challenge_prompt("z" * 500_000)

        assert "COMPLETE change under review" not in prompt

    def test_a_whole_diff_still_claims_the_perimeter(self) -> None:
        prompt = _build_review_challenge_prompt("a small, complete diff")

        assert "COMPLETE change under review" in prompt


class TestWritingTheSubjectFile:
    def test_it_writes_the_raw_subject(self, tmp_path: Path) -> None:
        from hivepilot.orchestrator import _write_review_subject

        path = _write_review_subject(tmp_path, "the whole diff")

        assert path is not None
        assert (tmp_path / path).read_text() == "the whole diff"

    def test_a_write_failure_is_not_fatal(self, tmp_path: Path) -> None:
        """A review must still happen if the workspace is read-only. The
        file is a convenience, not the subject itself."""
        from hivepilot.orchestrator import _write_review_subject

        assert _write_review_subject(tmp_path / "does-not-exist", "diff") is None
