"""Tests for the agent_report parser (parse_agent_report)."""

from __future__ import annotations

from hivepilot.services.agent_report import AgentReport, parse_agent_report

# ---------------------------------------------------------------------------
# Full structured input — markdown header style
# ---------------------------------------------------------------------------


def test_full_structured_markdown_style() -> None:
    """All fields present using ## headers; links extracted from vault path."""
    text = (
        "## status\n"
        "NEEDS_HUMAN\n"
        "## summary\n"
        "- bullet a\n"
        "- bullet b\n"
        "## decisions\n"
        "Go with option B\n"
        "## blockers\n"
        "None\n"
        "## next_handoff\n"
        "Deploy to staging\n"
        "## confidence\n"
        "high\n"
        "## links\n"
        "/home/jeromesoyer/Documents/Github/jsoyer/obsidian-vault/Noxys/artifact.md\n"
    )
    result = parse_agent_report(text)
    assert result.status == "NEEDS_HUMAN"
    assert len(result.summary) == 2
    assert result.summary[0] == "bullet a"
    assert result.summary[1] == "bullet b"
    assert result.next_handoff == "Deploy to staging"
    assert any(
        "/home/jeromesoyer/Documents/Github/jsoyer/obsidian-vault/Noxys/artifact.md" in link
        for link in result.links
    )


# ---------------------------------------------------------------------------
# Plain non-structured text
# ---------------------------------------------------------------------------


def test_plain_unstructured_text() -> None:
    """No fields detected → summary empty, raw preserved."""
    text = "This is just some random agent output with no structure at all."
    result = parse_agent_report(text)
    assert result.summary == []
    assert result.raw == text


# ---------------------------------------------------------------------------
# Colon-style parsing
# ---------------------------------------------------------------------------


def test_colon_style_parsing() -> None:
    """status: PASS and summary bullets via colon style."""
    text = "status: PASS\nsummary:\n- x\n- y\n"
    result = parse_agent_report(text)
    assert result.status == "PASS"
    assert result.summary == ["x", "y"]


# ---------------------------------------------------------------------------
# Mixed-case field names
# ---------------------------------------------------------------------------


def test_mixed_case_status() -> None:
    """Field matching is case-insensitive."""
    text = "Status: BLOCKED\n"
    result = parse_agent_report(text)
    assert result.status == "BLOCKED"


# ---------------------------------------------------------------------------
# URL extraction into links
# ---------------------------------------------------------------------------


def test_url_extracted_into_links() -> None:
    """HTTP URLs in the text should be collected in links."""
    text = "## status\nPASS\n## summary\n- done\n\nSee https://example.com/report for details.\n"
    result = parse_agent_report(text)
    assert any("https://example.com/report" in link for link in result.links)


# ---------------------------------------------------------------------------
# File path extraction
# ---------------------------------------------------------------------------


def test_file_path_extracted_into_links() -> None:
    """Paths matching /…/*.md pattern appear in links."""
    text = "## status\nPASS\nresult at /home/user/docs/report.md\n"
    result = parse_agent_report(text)
    assert any("/home/user/docs/report.md" in link for link in result.links)


# ---------------------------------------------------------------------------
# AgentReport dataclass defaults
# ---------------------------------------------------------------------------


def test_agent_report_defaults() -> None:
    """All optional fields default to empty string / empty list."""
    report = AgentReport(
        status="",
        summary=[],
        decisions="",
        blockers="",
        next_handoff="",
        confidence="",
        links=[],
        raw="",
    )
    assert report.summary == []
    assert report.links == []
