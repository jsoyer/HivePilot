"""Tests for Telegram-safe formatting helpers."""

from __future__ import annotations

import pytest

from hivepilot.services import notification_service as ns
from hivepilot.services.agent_report import to_telegram_text

# ---------------------------------------------------------------------------
# Unit tests for to_telegram_text
# ---------------------------------------------------------------------------


def test_to_telegram_text_strips_headings() -> None:
    """Heading markers are removed; heading text is kept."""
    result = to_telegram_text("## Summary\n### Sub")
    assert result == "Summary\nSub"


def test_to_telegram_text_strips_hrules() -> None:
    """Horizontal rules are removed entirely."""
    result = to_telegram_text("---\n***\n___")
    # After stripping all three hr lines, only blank lines remain
    stripped = result.strip()
    assert stripped == ""


def test_to_telegram_text_strips_table_rows() -> None:
    """Markdown table rows (including separator rows) are removed."""
    result = to_telegram_text("| col1 | col2 |\n|------|------|")
    stripped = result.strip()
    assert stripped == ""


def test_to_telegram_text_keeps_bullets() -> None:
    """Bullet lines (- and *) are preserved unchanged."""
    text = "- item one\n* item two"
    result = to_telegram_text(text)
    assert "- item one" in result
    assert "* item two" in result


def test_to_telegram_text_collapses_blank_lines() -> None:
    """Three or more consecutive blank lines collapse to one blank line."""
    result = to_telegram_text("a\n\n\n\nb")
    assert result == "a\n\nb"


# ---------------------------------------------------------------------------
# Integration tests for stream_agent_turn rendering
# ---------------------------------------------------------------------------


@pytest.fixture
def rich_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Monkeypatch _send_telegram and set rich streaming settings."""
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "123", raising=False)
    return calls


def test_stream_agent_turn_no_tables_in_card(rich_capture: list[dict]) -> None:
    """Rich card strips markdown tables, heading markers, and caps at 5 bullets."""
    # Build a summary with a markdown table, heading, and 10 bullet points
    summary = (
        "## status\n"
        "PASS\n"
        "## summary\n"
        "- Bullet point one\n"
        "- Bullet point two\n"
        "- Bullet point three\n"
        "- Bullet point four\n"
        "- Bullet point five\n"
        "- Bullet point six (should be dropped)\n"
        "- Bullet point seven (should be dropped)\n"
        "- Bullet point eight (should be dropped)\n"
        "- Bullet point nine (should be dropped)\n"
        "- Bullet point ten (should be dropped)\n"
        "## decisions\n"
        "| file | action |\n"
        "|------|--------|\n"
        "| foo.py | added |\n"
    )

    ns.stream_agent_turn(actor="Developer", summary=summary)

    assert len(rich_capture) == 1
    msg = rich_capture[0]["msg"]

    # No pipe characters from table rows
    assert "|" not in msg, f"Table pipe found in card: {msg!r}"

    # At most 5 bullets (count '•' characters)
    bullet_count = msg.count("•")
    assert bullet_count <= 5, f"Too many bullets ({bullet_count}): {msg!r}"

    # Total length reasonable
    assert len(msg) <= 800, f"Card too long ({len(msg)}): {msg!r}"


def test_stream_agent_turn_artifact_link_in_card(rich_capture: list[dict]) -> None:
    """Rendered card contains artifact .md path when present in links."""
    summary = (
        "## status\n"
        "PASS\n"
        "## summary\n"
        "- Implementation complete\n"
        "- Tests passing\n"
        "## links\n"
        "- /path/to/vault/artifact.md\n"
    )

    ns.stream_agent_turn(actor="Developer", summary=summary)

    assert len(rich_capture) == 1
    msg = rich_capture[0]["msg"]

    # The artifact path should appear in the card
    assert "artifact.md" in msg, f"Artifact link not found in card: {msg!r}"
