"""A fair share is not enough: the verdict itself must survive the cut.

Fair-share truncation (#525) stopped one verbose stage from evicting every
other, which was a real defect. It does not fix run 639, and measuring that
honestly is the point of this file.

The measured offsets on that run:

    Victor (Reviewer)   8 352 chars, `## APPROVAL` at 5 503,
                        `status: REQUEST_CHANGES` at 6 134
    Hugo (CISO)        12 289 chars, `clearance` at 7 022

With five stages against an 8 000-character budget each chunk gets ~1 600 --
800 of head, 800 of tail. Victor's verdict sits at 5 503 of 8 352: squarely
in the elided middle. Hugo's at 7 022 of 12 289: likewise. So the release
manager STILL adjudicates without seeing either verdict, and still cannot
tell a truncated adjudication from an absent one.

Every role's output contract in this deployment mandates a `status:` field.
That is the one anchor the engine can rely on, so truncation now retains the
region around the LAST such marker in each chunk, between the head and the
tail. The verdict is the reason the stage ran; it is the last thing that
should be dropped, not the first.

Run 639's correct outcome was still BLOCK -- Victor asked for changes. The
defect was never the decision, it was that the gate reached it by accident,
reporting two verdicts as MISSING when both had been given.
"""

from __future__ import annotations

from hivepilot.orchestrator import build_prior_context

ELISION = "…[truncated]…"


def _report(header: str, verdict: str, size: int, verdict_at: int) -> str:
    """A stage report of *size* chars carrying *verdict* at *verdict_at*."""
    filler = "x" * size
    body = filler[:verdict_at] + verdict + filler[verdict_at + len(verdict) :]
    return f"{header}\n{body}"


class TestTheVerdictSurvivesTruncation:
    def test_the_run_639_reviewer_verdict_reaches_the_gate(self):
        """The measured case, verbatim: five stages, budget 8 000, and the
        reviewer's `status:` at offset 6 134 of 8 352."""
        chunks = [
            _report("## CTO", "status: PASS", 15_251, 9_000),
            _report("## Developer", "status: PASS", 4_527, 3_000),
            _report("## Reviewer", "status: REQUEST_CHANGES", 8_352, 6_134),
            _report("## CISO", "status: CLEARED", 12_289, 7_022),
            _report("## QA", "status: BLOCKED", 26_374, 20_000),
        ]

        out = build_prior_context(chunks, mode="cap", max_chars=8_000)

        assert out is not None
        assert "status: REQUEST_CHANGES" in out, "the reviewer's verdict was truncated away"
        assert "status: CLEARED" in out, "the CISO's clearance was truncated away"

    def test_every_stage_still_keeps_its_header(self):
        """The #525 guarantee must not regress while adding this one."""
        chunks = [
            _report("## CTO", "status: PASS", 15_251, 9_000),
            _report("## QA", "status: BLOCKED", 26_374, 20_000),
        ]

        out = build_prior_context(chunks, mode="cap", max_chars=2_000)

        assert "## CTO" in out
        assert "## QA" in out

    def test_the_last_marker_wins_when_a_report_discusses_several(self):
        """A report that quotes an earlier stage's `status:` before stating its
        own must yield its OWN verdict -- the concluding one."""
        chunk = (
            "## Reviewer\n"
            + "a" * 3_000
            + "status: QUOTED_FROM_UPSTREAM"
            + "b" * 3_000
            + "status: MY_OWN_VERDICT"
            + "c" * 3_000
        )

        out = build_prior_context([chunk], mode="cap", max_chars=1_200)

        assert "status: MY_OWN_VERDICT" in out

    def test_a_chunk_without_any_marker_is_unharmed(self):
        """No marker is not an error -- the developer stage has no verdict.
        It keeps the plain head/tail treatment."""
        chunk = "## Developer\n" + "x" * 20_000 + "TAIL-MARKER"

        out = build_prior_context([chunk], mode="cap", max_chars=1_000)

        assert "## Developer" in out
        assert "TAIL-MARKER" in out
        assert ELISION in out

    def test_a_marker_already_inside_the_head_is_not_duplicated(self):
        chunk = "## Reviewer\nstatus: PASS\n" + "x" * 20_000

        out = build_prior_context([chunk], mode="cap", max_chars=2_000)

        assert out.count("status: PASS") == 1

    def test_content_that_fits_is_still_returned_verbatim(self):
        chunks = ["## A\nstatus: PASS", "## B\nstatus: BLOCKED"]

        out = build_prior_context(chunks, mode="cap", max_chars=8_000)

        assert out == "## A\nstatus: PASS\n\n## B\nstatus: BLOCKED"
