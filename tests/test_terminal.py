"""Untrusted text must not be able to drive the operator's terminal.

Three surfaces render text this codebase did not author: plugin-marketplace
metadata, skill descriptions, and — once the live role board lands — the
event stream a herdr pane follows. Each had grown its own regex, and they
did not agree: one covered C0 and DEL, the other also covered C1.

The disagreement mattered. 0x9B is a bare CSI that some terminals honour
without a preceding ESC, so the narrower pattern left the door open on a
surface whose whole job is displaying agent output.

Two dangers, one character class:

- **Escape sequences** — cursor movement, screen clearing, title setting.
- **Line forgery** — for a renderer that prints one record per line, an
  embedded newline lets untrusted text fabricate a line that looks like one
  the system emitted. An agent error message ending in a plausible status
  line would appear on the board as a real event.
"""

from __future__ import annotations

from hivepilot.utils.terminal import strip_control_chars


class TestEscapeSequences:
    def test_a_csi_escape_cannot_survive(self) -> None:
        assert strip_control_chars("\x1b[2Jwiped") == "[2Jwiped"

    def test_the_eight_bit_csi_is_stripped_too(self) -> None:
        """0x9B is CSI without a preceding ESC. The narrower of the two
        regexes this replaces would have let it through."""
        assert "\x9b" not in strip_control_chars("\x9b[31mred")

    def test_del_is_stripped(self) -> None:
        assert strip_control_chars("a\x7fb") == "ab"

    def test_a_title_sequence_loses_its_trigger(self) -> None:
        assert "\x1b" not in strip_control_chars("\x1b]0;pwned\x07")


class TestLineForgery:
    def test_a_newline_cannot_fabricate_a_record(self) -> None:
        """The board renders one event per line; an embedded newline would
        otherwise let an agent's error text impersonate an event."""
        forged = "boom\nstate.step review:ciso success"

        assert "\n" not in strip_control_chars(forged)

    def test_carriage_return_cannot_rewrite_the_line(self) -> None:
        assert "\r" not in strip_control_chars("real\rfake")

    def test_a_tab_cannot_break_column_alignment(self) -> None:
        assert "\t" not in strip_control_chars("a\tb")


class TestOrdinaryTextIsUntouched:
    def test_plain_ascii_passes_through(self) -> None:
        assert strip_control_chars("review:ciso success") == "review:ciso success"

    def test_accents_and_emoji_survive(self) -> None:
        """The pattern must not become a de-facto ASCII filter — role names
        and agent prose are not ASCII here."""
        assert strip_control_chars("revue terminée ✅") == "revue terminée ✅"

    def test_an_empty_string_stays_empty(self) -> None:
        assert strip_control_chars("") == ""


class TestTheReplacementIsChosenByTheCaller:
    def test_deleting_is_the_default(self) -> None:
        assert strip_control_chars("a\x00b") == "ab"

    def test_prose_can_ask_for_a_space_instead(self) -> None:
        """Deleting silently welds two words together; a description reads
        better with the break preserved."""
        assert strip_control_chars("word\nword", replacement=" ") == "word word"
