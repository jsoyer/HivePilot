"""Make untrusted text safe to print into an operator's terminal.

Three places in this codebase render text they did not author: plugin
marketplace metadata, skill descriptions, and the event stream a herdr pane
follows. Each had grown its own regex, and they did not agree. This is the
one they share.

Two distinct dangers, both closed by removing the same character class:

**Escape sequences.** A control character can move the cursor, clear the
screen, or set the window title. The pattern covers C0 (0x00-0x1F) and DEL,
and also C1 (0x80-0x9F) -- the 8-bit forms, of which 0x9B is a bare CSI that
some terminals honour without a preceding ESC. Stripping C0 alone leaves
that door open, and did.

**Line forgery.** For anything rendered one record per line, an embedded
newline lets untrusted text fabricate a line that looks like a record the
system emitted -- an agent's error message ending in a plausible status line
would appear on a board as a real event. Newline is a C0 character, so the
same strip closes this too, which is why a single-line renderer must not
exempt it.
"""

from __future__ import annotations

import re

__all__ = ["CONTROL_CHARS_RE", "strip_control_chars"]

#: C0 + DEL + C1. See the module docstring for why C1 belongs here.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def strip_control_chars(value: str, *, replacement: str = "") -> str:
    """Return *value* with every control character removed.

    ``replacement`` is what each stripped character becomes. Use ``""`` for
    identifier-like fields, where an injected space would corrupt the value,
    and ``" "`` for prose, where deleting silently welds two words together.
    """
    return CONTROL_CHARS_RE.sub(replacement, value)
