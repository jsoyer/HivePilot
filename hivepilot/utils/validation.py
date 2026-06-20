"""Input validation & prompt-injection defenses.

Policy: strong but non-blocking.
- ``sanitize_prompt`` ALWAYS neutralizes control / invisible characters and
  normalizes unicode — it never rejects content.
- ``check_prompt_injection`` DETECTS known injection patterns and returns their
  names for logging; it does not reject (callers decide what to do).
- ``validate_note`` enforces hard limits on short metadata fields (token notes)
  and rejects clearly-abusive input.
"""

from __future__ import annotations

import re
import unicodedata

# Generous cap — long enough for real prompts, short enough to bound abuse.
MAX_PROMPT_LEN = 16000
MAX_NOTE_LEN = 200

# C0/C1 control characters except tab (\x09), newline (\x0a), carriage return (\x0d).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Zero-width / bidi-override characters frequently used to smuggle instructions.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")

# Known prompt-injection / jailbreak signatures. Detection only (non-blocking).
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous",
        re.compile(
            r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+"
            r"(?:instructions?|prompts?|context)",
            re.I,
        ),
    ),
    (
        "disregard",
        re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior|above|the\s+system)", re.I),
    ),
    (
        "override_role",
        re.compile(
            r"you\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?(?:dan|jailbroken|unrestricted|evil)", re.I
        ),
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"(?:reveal|print|show|repeat|leak|expose)\s+(?:your\s+|the\s+)?"
            r"(?:system\s+prompt|initial\s+(?:prompt|instructions)|instructions)",
            re.I,
        ),
    ),
    ("jailbreak", re.compile(r"developer\s+mode|jailbreak|\bDAN\b", re.I)),
    (
        "exfiltration",
        re.compile(
            r"(?:send|post|exfiltrate|upload|email)\b.{0,40}?"
            r"(?:secret|token|credential|api[_\s-]?key|password|\.env)",
            re.I,
        ),
    ),
    (
        "instruction_terminator",
        re.compile(r"-{3,}\s*end\s+of\s+(?:prompt|instructions)|#{2,}\s*system\b", re.I),
    ),
    ("role_injection", re.compile(r"^\s*(?:system|assistant)\s*:", re.I | re.M)),
]


def sanitize_prompt(text: str) -> str:
    """Return *text* with control/invisible chars removed and unicode normalized.

    Never rejects — this is the always-on hygiene layer.
    """
    if not text:
        return text
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _INVISIBLE.sub("", text)
    return text.strip()


def check_prompt_injection(text: str) -> list[str]:
    """Return the names of injection patterns matched in *text* (empty if clean).

    Detection only — callers log / score; the request is not blocked here.
    """
    if not text:
        return []
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]


def validate_note(note: str | None) -> str | None:
    """Validate a short metadata note (e.g. an API-token note).

    Returns the sanitized note, or ``None`` when *note* is ``None``. Raises
    ``ValueError`` on abuse (too long, embedded line breaks).
    """
    if note is None:
        return None
    cleaned = sanitize_prompt(note)
    if len(cleaned) > MAX_NOTE_LEN:
        raise ValueError(f"note exceeds maximum length of {MAX_NOTE_LEN} characters")
    if "\n" in note or "\r" in note:
        raise ValueError("note must not contain line breaks")
    return cleaned
