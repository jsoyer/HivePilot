"""The budget was an order of magnitude smaller than the pipeline it served.

Everything upstream of this file -- fair shares, verdict retention -- is
machinery for surviving a cut. Worth having, but it answers the wrong
question. The right one is why the cut happens at all.

Measured, run 639, the whole pipeline:

    8 stages, 75 728 characters total  (~19 000 tokens)
    max_prior_context_chars = 8 000    (~2 000 tokens)

So 90% of the run's own history was discarded to save roughly seventeen
thousand input tokens on the closing stages -- cents, on a run costing
several euros. A single stage did not fit: the CISO alone wrote 12 289
characters and QA 26 374.

Truncation has three real justifications -- the context window, cost (stage N
carries stages 1..N-1, so the bill grows quadratically), and attention
quality. None of them argue for 8 000 characters against a modern window.
That number is a leftover from small ones.

The default now holds a full run of this shape with headroom, and the cut
becomes what it should always have been: a backstop against a runaway, not
routine behaviour. The fair-share and verdict-retention rules stay, because
a backstop that mangles the verdict is not a backstop.
"""

from __future__ import annotations

from hivepilot.config import settings
from hivepilot.orchestrator import build_prior_context

# The eight interaction sizes recorded for run 639, in stage order.
RUN_639 = [272, 960, 15_251, 4_527, 8_352, 12_289, 26_374, 7_703]


class TestTheDefaultBudgetHoldsARealRun:
    def test_the_default_is_not_smaller_than_a_single_stage(self):
        """QA wrote 26 374 characters. A budget under that cannot represent
        even one stage whole, which is how five of them disappeared."""
        assert settings.max_prior_context_chars > max(RUN_639)

    def test_run_639_would_not_have_been_truncated_at_all(self):
        chunks = ["x" * n for n in RUN_639]
        joined = len("\n\n".join(chunks))

        out = build_prior_context(chunks, mode="cap", max_chars=settings.max_prior_context_chars)

        assert joined <= settings.max_prior_context_chars
        assert out == "\n\n".join(chunks), "a run this size must pass through untouched"

    def test_the_backstop_still_exists(self):
        """Raising the ceiling must not remove it -- a runaway stage still has
        to be bounded, and still has to keep its verdict."""
        runaway = "## Runaway\n" + "x" * 400_000 + "status: BLOCKED" + "y" * 400_000

        out = build_prior_context([runaway], mode="cap", max_chars=settings.max_prior_context_chars)

        assert out is not None
        assert len(out) < len(runaway)
        assert "status: BLOCKED" in out
