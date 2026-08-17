"""One verbose stage must not evict every earlier stage from the context.

Measured on run 639, the noxys delivery run, from the real interaction sizes:

    Blaise (CTO)              15 251
    Gustave (Developer)        4 527
    Victor (Reviewer)          8 352
    Hugo (CISO)               12 289   <- clearance verdict at offset 7 022
    Marie (QA)                26 374

`cap` mode joined all of it and kept the TAIL of `max_prior_context_chars`
(8 000 by default). QA alone is more than three times that budget, so the
window never reached back past QA: the release manager received the tail of
one stage's output and nothing else.

She then blocked the release with

    security: MISSING -> not satisfied -- no `clearance` verdict

which was false. Hugo had cleared it. His verdict had simply been truncated
away, and a gate that cannot see an adjudication reports it as absent --
indistinguishable, from where it sits, from a reviewer who never adjudicated
at all.

So the cap becomes a FAIR SHARE: every chunk is guaranteed a slice, and each
slice keeps its head and its tail with an explicit elision between them --
the head because these reports lead with their structured verdict (`##
STATUS`, `## REVIEW_REPORT`), the tail because that is where they conclude.

This does not promise that an arbitrary mid-document field survives; no
truncation scheme can. It promises something weaker and checkable: no stage
disappears entirely because a later one was verbose.
"""

from __future__ import annotations

from hivepilot.orchestrator import build_prior_context

ELISION = "…[truncated]…"


def _chunk(header: str, size: int) -> str:
    body = "x" * max(0, size - len(header) - 1)
    return f"{header}\n{body}"


class TestNoStageIsEvictedByAVerboseNeighbour:
    def test_the_run_639_shape_keeps_every_stage(self):
        """The measured case: five stages, the last one 26 KB, budget 8 000."""
        chunks = [
            _chunk("## CTO", 15_251),
            _chunk("## Developer", 4_527),
            _chunk("## Reviewer", 8_352),
            _chunk("## CISO", 12_289),
            _chunk("## QA", 26_374),
        ]

        out = build_prior_context(chunks, mode="cap", max_chars=8_000)

        assert out is not None
        for header in ("## CTO", "## Developer", "## Reviewer", "## CISO", "## QA"):
            assert header in out, f"{header} was evicted by a later stage"

    def test_the_budget_is_respected(self):
        chunks = [_chunk(f"## S{i}", 20_000) for i in range(5)]

        out = build_prior_context(chunks, mode="cap", max_chars=8_000)

        assert out is not None
        # The elision markers and separators are bookkeeping on top of the
        # content budget; allow for them rather than pretending they are free.
        overhead = len(chunks) * (len(ELISION) + 4)
        assert len(out) <= 8_000 + overhead

    def test_truncation_is_announced_not_silent(self):
        """A silent cut reads as 'the stage said only this'. It must not."""
        chunks = [_chunk("## A", 20_000), _chunk("## B", 20_000)]

        out = build_prior_context(chunks, mode="cap", max_chars=1_000)

        assert ELISION in out

    def test_a_short_chunk_is_not_cut_at_all(self):
        """A stage that already fits keeps every character -- and its unused
        budget is not wasted on it."""
        short = _chunk("## Short", 50)
        long = _chunk("## Long", 40_000)

        out = build_prior_context([short, long], mode="cap", max_chars=8_000)

        assert short in out
        assert ELISION in out


class TestTheUnchangedContracts:
    def test_content_that_fits_is_returned_verbatim(self):
        chunks = ["## A\naaa", "## B\nbbb"]

        assert build_prior_context(chunks, mode="cap", max_chars=8_000) == "## A\naaa\n\n## B\nbbb"

    def test_empty_chunks_still_return_none(self):
        assert build_prior_context([], mode="cap", max_chars=8_000) is None

    def test_full_mode_is_untouched(self):
        chunks = [_chunk("## A", 20_000), _chunk("## B", 20_000)]

        assert build_prior_context(chunks, mode="full", max_chars=10) == "\n\n".join(chunks)

    def test_synthesis_mode_is_untouched(self):
        synthesis = "## Plan Synthesis\nthe plan"
        last = "## Last\nthe last"

        out = build_prior_context([synthesis, "## Middle\nm", last], mode="synthesis", max_chars=10)

        assert out == f"{synthesis}\n\n{last}"

    def test_a_single_chunk_over_budget_keeps_head_and_tail(self):
        """The old behaviour kept only the tail. The head carries `## STATUS`
        and the report's own structure, which is exactly what a gate reads
        first."""
        chunk = "## STATUS\nHEAD-MARKER\n" + ("x" * 20_000) + "\nTAIL-MARKER"

        out = build_prior_context([chunk], mode="cap", max_chars=2_000)

        assert "HEAD-MARKER" in out
        assert "TAIL-MARKER" in out
        assert ELISION in out
