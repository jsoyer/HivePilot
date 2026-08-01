"""The approval card has to be readable — it is the only thing the operator sees.

From a real checkpoint (run #280) the card rendered:

    ## Blaise (CTO) (CTO)
    ## TECHNICAL_SPEC
    ...
    - **PM (2) → `product_spec`:** explicitly no in-scope work, no user sto...
    📂 Full plan: in the Obsidian vault.

Three things wrong at once: the agent's title doubled, the excerpt cut
mid-word, and a pointer to "the vault" that names no folder. The operator is
being asked to approve advancing a pipeline on the strength of that.
"""

from __future__ import annotations

from hivepilot.orchestrator import _build_checkpoint_details


def test_the_technical_heading_is_not_repeated_in_the_excerpt():
    """The card already states whose stage this is."""
    details = _build_checkpoint_details(
        completed=["CEO Intake"],
        next_stage="Implementation",
        prior_chunks=["## Blaise (CTO)\nTECHNICAL_SPEC\n\nnone — nothing to build."],
        group_mode=False,
        components=[],
    )
    assert "## Blaise (CTO)" not in details
    assert "nothing to build" in details


def test_a_long_excerpt_is_cut_on_a_line_not_mid_word():
    body = "\n".join(f"line {i} with some real content in it" for i in range(200))
    details = _build_checkpoint_details(
        completed=[],
        next_stage="Implementation",
        prior_chunks=[f"## Blaise (CTO)\n{body}"],
        group_mode=False,
        components=[],
    )
    excerpt = details.split("*Plan excerpt:*\n", 1)[1]
    kept = excerpt.rsplit("\n…", 1)[0]
    assert kept.endswith("in it"), f"cut mid-line: {kept[-40:]!r}"


def test_a_short_excerpt_is_left_whole():
    details = _build_checkpoint_details(
        completed=[],
        next_stage="Implementation",
        prior_chunks=["## Blaise (CTO)\nAll clear, proceed."],
        group_mode=False,
        components=[],
    )
    assert "All clear, proceed." in details
    assert "…" not in details


def test_a_heading_only_chunk_produces_no_empty_excerpt_block():
    """An excerpt header with nothing under it is worse than no header."""
    details = _build_checkpoint_details(
        completed=[],
        next_stage="Implementation",
        prior_chunks=["## Blaise (CTO)"],
        group_mode=False,
        components=[],
    )
    assert "Plan excerpt" not in details


def test_the_next_stage_is_always_stated():
    details = _build_checkpoint_details(
        completed=["CEO Intake", "Product Spec"],
        next_stage="Implementation",
        prior_chunks=[],
        group_mode=False,
        components=[],
    )
    assert "Implementation" in details
    assert "CEO Intake" in details
