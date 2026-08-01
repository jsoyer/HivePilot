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


# ---------------------------------------------------------------------------
# The two facts that decide the answer
# ---------------------------------------------------------------------------


def test_status_and_blockers_are_surfaced():
    """Neither was shown at all — an operator approved past an unread verdict."""
    chunk = (
        "## Colette (Release Manager)\n"
        "- status: BLOCK\n"
        "- summary:\n"
        "  - reviewer verdict never issued\n"
        "- blockers: security clearance withheld\n"
    )
    details = _build_checkpoint_details(
        completed=["Review"],
        next_stage="PR Approval",
        prior_chunks=[chunk],
        group_mode=False,
        components=[],
    )
    assert "BLOCK" in details
    assert "security clearance withheld" in details


def test_an_absent_blocker_is_not_rendered_as_the_word_none():
    chunk = "## Blaise (CTO)\n- status: PASS\n- summary:\n  - all clear\n- blockers: none\n"
    details = _build_checkpoint_details(
        completed=[],
        next_stage="Implementation",
        prior_chunks=[chunk],
        group_mode=False,
        components=[],
    )
    assert "Blockers" not in details
    assert "PASS" in details


def test_the_excerpt_skips_preamble_and_starts_at_the_content():
    """The real failure: budget spent on prose, cut where substance began."""
    preamble = " ".join(["Verified directly this cycle, not inherited."] * 40)
    chunk = f"## Blaise (CTO)\n{preamble}\n\n## TECHNICAL_SPEC\n\nComponent A talks to B."
    details = _build_checkpoint_details(
        completed=[],
        next_stage="Implementation",
        prior_chunks=[chunk],
        group_mode=False,
        components=[],
    )
    assert "TECHNICAL_SPEC" in details
    assert "Component A talks to B." in details, "the substance must survive the cut"
    assert "Verified directly this cycle" not in details, "preamble must be skipped"
