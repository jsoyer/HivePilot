"""Tests for the Telegram no-truncation split + safe-HTML formatting helpers.

Covers:
- _split_for_telegram: paragraph/line/word-boundary splitting, entity-aware
  (never cuts inside a <pre>/<code>/<b> tag), capped chunk count.
- _format_for_telegram_html: safe Markdown -> Telegram HTML subset,
  escaping, and graceful handling of unbalanced markdown.
- _strip_html: plain-text degrade used by the per-chunk 400 fallback.
"""

from __future__ import annotations

import re

from hivepilot.services import notification_service as ns

# ---------------------------------------------------------------------------
# _split_for_telegram — plain text (html_aware=False)
# ---------------------------------------------------------------------------


def test_split_short_text_returns_single_chunk() -> None:
    text = "hello world"
    assert ns._split_for_telegram(text) == [text]


def test_split_12kb_text_multiple_chunks_within_limit() -> None:
    paragraph = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20
    text = "\n\n".join(paragraph for _ in range(10))  # ~12KB
    assert len(text) > 10_000

    chunks = ns._split_for_telegram(text, html_aware=False)

    assert len(chunks) > 1
    for chunk in chunks:
        # Strip the "(i/N)" continuation marker before checking the limit.
        body = re.sub(r"\n\n\(\d+/\d+\)$", "", chunk)
        assert len(body) <= ns._SPLIT_LIMIT


def test_split_12kb_text_breaks_on_paragraph_or_line_boundary() -> None:
    paragraph = "word " * 100
    text = "\n\n".join(paragraph for _ in range(30))
    chunks = ns._split_for_telegram(text, html_aware=False)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        body = re.sub(r"\n\n\(\d+/\d+\)$", "", chunk)
        # Every chunk but the last ends right at a paragraph break, a line
        # break, or a word (space) boundary — never mid-word.
        assert body.endswith("\n\n") or body.endswith("\n") or body.endswith(" ")


def test_split_reassembly_preserves_all_content() -> None:
    """Concatenating chunk bodies (continuation markers stripped) reproduces
    the original text — nothing is silently dropped when under the cap."""
    paragraph = "alpha beta gamma delta epsilon. " * 50
    text = "\n\n".join(paragraph for _ in range(8))
    chunks = ns._split_for_telegram(text, html_aware=False)
    bodies = [re.sub(r"\n\n\(\d+/\d+\)$", "", c) for c in chunks]
    assert "".join(bodies) == text


def test_split_huge_text_capped_at_max_chunks_with_note() -> None:
    """A pathological (multi-hundred-KB) dump is capped, not sent as dozens
    of messages, and the drop is explicitly noted."""
    text = "x " * 200_000  # ~400KB
    chunks = ns._split_for_telegram(text, html_aware=False)
    assert len(chunks) <= ns._MAX_CHUNKS
    assert "truncated" in chunks[-1]


def test_split_normal_short_message_single_chunk_no_marker() -> None:
    text = "short status update"
    chunks = ns._split_for_telegram(text, html_aware=False)
    assert chunks == [text]
    assert "(1/1)" not in chunks[0]


# ---------------------------------------------------------------------------
# _split_for_telegram — entity-aware (html_aware=True)
# ---------------------------------------------------------------------------


def _tags_balanced(chunk: str) -> bool:
    """A chunk is entity-safe if every tag opened in it is also closed in
    it (our splitter always closes+reopens across boundaries, so each
    chunk must be independently well-formed)."""
    stack: list[str] = []
    for m in ns._TAG_TOKEN_RE.finditer(chunk):
        name = m.group(2)
        if name not in ns._KNOWN_TAGS:
            continue
        if m.group(1) == "/":
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack


def test_split_pre_block_spanning_boundary_stays_balanced() -> None:
    body = "line of code\n" * 500
    html_text = f"header\n\n<pre>{body}</pre>\n\nfooter"
    chunks = ns._split_for_telegram(html_text, limit=200, max_chunks=50, html_aware=True)
    assert len(chunks) > 1
    for chunk in chunks:
        assert _tags_balanced(chunk), chunk[:80]


def test_split_bold_spanning_boundary_stays_balanced() -> None:
    html_text = "<b>" + ("word " * 1000) + "</b>"
    chunks = ns._split_for_telegram(html_text, limit=150, max_chunks=50, html_aware=True)
    assert len(chunks) > 1
    for chunk in chunks:
        assert _tags_balanced(chunk), chunk[:80]


def test_split_html_aware_never_mistakes_plain_angle_brackets_for_tags() -> None:
    """html_aware=False must be used for genuinely plain text — a literal
    '<Foo>' (e.g. a generic type) must never grow a spurious closing tag."""
    text = "List<Item> result. " * 400
    chunks = ns._split_for_telegram(text, html_aware=False)
    assert "</Item>" not in "".join(chunks)
    assert "".join(re.sub(r"\n\n\(\d+/\d+\)$", "", c) for c in chunks) == text


# ---------------------------------------------------------------------------
# _format_for_telegram_html
# ---------------------------------------------------------------------------


def test_format_header_becomes_bold() -> None:
    """A top-level '#' header renders as plain bold; the raw '#' marker is
    gone (never left as a literal '##' dump)."""
    result = ns._format_for_telegram_html("# Status\nPASS")
    assert "<b>Status</b>" in result
    assert "#" not in result


def test_format_subheader_gets_nesting_marker() -> None:
    """A '##'+ sub-header gets a visual "▸ " marker since Telegram HTML has
    no real heading levels to distinguish nesting."""
    result = ns._format_for_telegram_html("## Status\nPASS")
    assert "<b>▸ Status</b>" in result
    assert "##" not in result


def test_format_bold_markdown() -> None:
    assert ns._format_for_telegram_html("**important**") == "<b>important</b>"
    assert ns._format_for_telegram_html("__also bold__") == "<b>also bold</b>"


def test_format_inline_code() -> None:
    result = ns._format_for_telegram_html("run `pytest -q` now")
    assert "<code>pytest -q</code>" in result


def test_format_fenced_code_block() -> None:
    result = ns._format_for_telegram_html("```\ndef f():\n    pass\n```")
    assert "<pre>" in result and "</pre>" in result
    assert "def f():" in result


def test_format_bullets_become_bullet_char() -> None:
    result = ns._format_for_telegram_html("- one\n- two")
    assert "• one" in result
    assert "• two" in result


def test_format_escapes_angle_brackets_and_ampersand() -> None:
    result = ns._format_for_telegram_html("a < b & c > d")
    assert "&lt;" in result
    assert "&amp;" in result
    assert "&gt;" in result
    # No stray literal '<'/'>' outside of the escaped entities.
    assert "< b" not in result


def test_format_unbalanced_bold_never_produces_dangling_tag() -> None:
    """An unbalanced '**' must be escaped as plain text, never a half-open
    <b> that would make Telegram reject the whole message with a 400."""
    result = ns._format_for_telegram_html("this is **not closed")
    assert result.count("<b>") == result.count("</b>")
    assert "**" in result  # left as literal, unconverted


def test_format_does_not_reprocess_inside_code_block() -> None:
    """A '#' or '**' literally inside a fenced code block must stay literal
    (not get converted into <b> by a later pass reaching into the stash)."""
    result = ns._format_for_telegram_html("```\n# not a header\n**not bold**\n```")
    assert "<pre>" in result
    inner = result.split("<pre>", 1)[1].rsplit("</pre>", 1)[0]
    assert "# not a header" in inner
    assert "**not bold**" in inner


def test_format_italic_markdown() -> None:
    assert ns._format_for_telegram_html("*important*") == "<i>important</i>"
    assert ns._format_for_telegram_html("_also italic_") == "<i>also italic</i>"


def test_format_blockquote() -> None:
    result = ns._format_for_telegram_html("> quoted wisdom")
    assert result == "<blockquote>quoted wisdom</blockquote>"


def test_format_horizontal_rule_becomes_separator() -> None:
    result = ns._format_for_telegram_html("above\n---\nbelow")
    assert "---" not in result
    assert "──────────" in result


def test_format_image_keeps_alt_text_only() -> None:
    result = ns._format_for_telegram_html("see ![a screenshot](https://example.com/x.png) below")
    assert "a screenshot" in result
    assert "example.com" not in result
    assert "![" not in result


def test_format_numbered_list_kept_numbered() -> None:
    result = ns._format_for_telegram_html("1. first\n2. second")
    assert "1. first" in result
    assert "2. second" in result


def test_format_nested_bullets_get_indent() -> None:
    result = ns._format_for_telegram_html("- top\n  - nested")
    lines = result.split("\n")
    assert "• top" in lines
    assert "  • nested" in lines


def test_format_table_becomes_aligned_pre_block_not_pipe_mess() -> None:
    md_table = "| file | action |\n|------|--------|\n| foo.py | added |\n| bar.py | removed |\n"
    result = ns._format_for_telegram_html(md_table)
    assert "<pre>" in result and "</pre>" in result
    assert "|" not in result
    inner = result.split("<pre>", 1)[1].rsplit("</pre>", 1)[0]
    rows = inner.split("\n")
    assert len(rows) == 3  # header + 2 data rows (separator row dropped)
    # Columns are padded to a fixed width, so every rendered row has the
    # exact same total length — that's what makes it read as an aligned
    # block instead of a ragged pipe dump.
    row_lengths = {len(r) for r in rows}
    assert len(row_lengths) == 1


def test_format_collapses_three_plus_blank_lines() -> None:
    result = ns._format_for_telegram_html("a\n\n\n\nb")
    assert result == "a\n\nb"


def test_format_representative_agent_report_is_clean_valid_html() -> None:
    """A REALISTIC agent hand-off — a header, a bullet list, inline code, a
    fenced code block, a small table, and a blockquote — converts to a
    clean report: every markdown marker is gone, tags are balanced, and
    the table renders as an aligned block instead of a broken pipe mess."""
    agent_output = (
        "## Summary\n"
        "Implemented the **auth middleware** using `FastAPI` dependencies.\n\n"
        "- Added `require_auth` dependency\n"
        "- Wired it into all `/v1` routes\n"
        "- Updated tests\n\n"
        "```python\n"
        "@app.get('/v1/secure')\n"
        "def secure(user=Depends(require_auth)):\n"
        "    return user\n"
        "```\n\n"
        "| file | action |\n"
        "|------|--------|\n"
        "| auth.py | added |\n"
        "| routes.py | modified |\n\n"
        "> Reviewed by the CISO — looks solid.\n"
    )

    result = ns._format_for_telegram_html(agent_output)

    # No raw markdown markers survive.
    for marker in ("##", "**", "|------|", "` "):
        assert marker not in result, f"raw markdown leaked: {marker!r} in {result!r}"

    # Structural conversions all landed.
    assert "<b>▸ Summary</b>" in result
    assert "<b>auth middleware</b>" in result
    assert "<code>FastAPI</code>" in result
    assert "• Added <code>require_auth</code> dependency" in result
    assert "<pre>" in result and "def secure(user=Depends(require_auth)):" in result
    assert "<blockquote>Reviewed by the CISO — looks solid.</blockquote>" in result

    # The table rendered as an aligned pre block, not a pipe dump.
    assert result.count("<pre>") == 2  # code block + table block
    table_block = result.split("<pre>")[2].split("</pre>")[0]
    assert "|" not in table_block
    assert "auth.py" in table_block and "added" in table_block

    # Every tag opened is also closed — a balanced, independently valid
    # HTML fragment (same invariant _split_for_telegram relies on).
    stack: list[str] = []
    for m in ns._TAG_TOKEN_RE.finditer(result):
        name = m.group(2)
        if m.group(1) == "/":
            assert stack and stack[-1] == name, f"unbalanced close </{name}> in {result!r}"
            stack.pop()
        else:
            stack.append(name)
    assert not stack, f"unbalanced open tag(s) left: {stack}"


# ---------------------------------------------------------------------------
# _strip_html — the per-chunk 400 fallback's plain-text degrade
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags_and_unescapes() -> None:
    html_chunk = "<b>Bold</b> &amp; <code>x</code>"
    assert ns._strip_html(html_chunk) == "Bold & x"
