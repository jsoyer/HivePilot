"""Parse structured agent reports into a typed dataclass.

Supports two input styles:
  - Markdown header style: ``## field_name\\nvalue``
  - Colon style: ``field_name: value``

Field matching is case-insensitive. Unknown / unstructured text is kept in
``raw`` and ``summary`` is left empty so callers can fall back to plain text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Regex patterns for link extraction
_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r"(?:/[\w.\-/]+)+\.(?:md|py|txt|json|yaml|yml|sh|ts|js|csv)")

# Patterns for Telegram-unsafe markdown constructs
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_HRULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,}|={3,})[ \t]*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

# Known structured field names (lower-case canonical keys)
_KNOWN_FIELDS = {
    "status",
    "summary",
    "decisions",
    "blockers",
    "next_handoff",
    "confidence",
    "links",
}


@dataclass
class AgentReport:
    """Structured representation of an agent's output report."""

    status: str
    summary: list[str]
    decisions: str
    blockers: str
    next_handoff: str
    confidence: str
    links: list[str]
    raw: str


def to_telegram_text(s: str) -> str:
    """Strip markdown elements that Telegram does not render.

    Removes:
    - Heading markers (``#``, ``##``, ``###``, etc.) — keeps the heading text.
    - Horizontal rules (lines of only ``---``, ``***``, ``___``, ``===``).
    - Markdown table rows (lines matching ``| ... |``).
    - Collapses 3+ consecutive blank lines down to 1 blank line.

    Preserves: ``*bold*``, ``_italic_``, bullet ``-`` / ``*``, inline code.
    """
    # Strip heading markers but keep the heading text
    result = _HEADING_RE.sub("", s)
    # Remove horizontal rules entirely
    result = _HRULE_RE.sub("", result)
    # Remove table rows (including separator rows like |---|)
    result = _TABLE_ROW_RE.sub("", result)
    # Collapse 3+ blank lines down to a single blank line
    result = _MULTI_BLANK_RE.sub("\n\n", result)
    return result


def _extract_links(text: str) -> list[str]:
    """Extract HTTP URLs and filesystem paths from *text*."""
    links: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;)")
        if url not in links:
            links.append(url)
    for m in _PATH_RE.finditer(text):
        path = m.group(0)
        if path not in links:
            links.append(path)
    return links


def _parse_bullets(block: str) -> list[str]:
    """Extract bullet items from a block of text.

    Handles both ``- item`` and ``* item`` prefixes.
    Applies :func:`to_telegram_text` to strip markdown constructs from each bullet.
    """
    bullets: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(to_telegram_text(stripped[2:].strip()))
        elif stripped.startswith("* "):
            bullets.append(to_telegram_text(stripped[2:].strip()))
    return bullets


def parse_agent_report(text: str) -> AgentReport:  # noqa: C901
    """Parse *text* into an :class:`AgentReport`.

    Tolerant parser: if nothing structured is found, returns an
    ``AgentReport`` with ``summary=[]`` and ``raw=text``.
    """
    fields: dict[str, str] = {}

    # --- Markdown header style: ## field_name\\nvalue block ---
    header_re = re.compile(r"^##\s+(\w+)\s*$", re.MULTILINE)
    header_matches = list(header_re.finditer(text))
    if header_matches:
        for i, m in enumerate(header_matches):
            key = m.group(1).lower()
            start = m.end()
            end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(text)
            value = text[start:end].strip()
            if key in _KNOWN_FIELDS:
                fields[key] = value

    # --- Colon style: field_name: value (single line or multi-line block) ---
    colon_re = re.compile(
        r"^(status|summary|decisions|blockers|next_handoff|confidence|links)[ \t]*:[ \t]*(.*)",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in colon_re.finditer(text):
        key = m.group(1).lower()
        value_inline = m.group(2).strip()
        if key not in fields:
            # Gather continuation bullet lines after this match
            continuation_lines: list[str] = []
            for line in text[m.end() :].splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") or stripped.startswith("* "):
                    continuation_lines.append(stripped)
                elif not stripped:
                    if continuation_lines:
                        break
                else:
                    break
            if continuation_lines:
                fields[key] = "\n".join(continuation_lines)
            elif value_inline:
                fields[key] = value_inline

    # --- Build AgentReport ---
    status = fields.get("status", "").strip()
    decisions = fields.get("decisions", "").strip()
    blockers = fields.get("blockers", "").strip()
    next_handoff = fields.get("next_handoff", "").strip()
    confidence = fields.get("confidence", "").strip()

    summary_raw = fields.get("summary", "")
    summary = _parse_bullets(summary_raw) if summary_raw else []

    # Extract links from the full text
    links_block = fields.get("links", "")
    links = _extract_links(text)
    # Also add bare paths/URLs explicitly listed in the links section
    for line in links_block.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if stripped and stripped not in links:
            links.append(stripped)

    return AgentReport(
        status=status,
        summary=summary,
        decisions=decisions,
        blockers=blockers,
        next_handoff=next_handoff,
        confidence=confidence,
        links=links,
        raw=text,
    )
