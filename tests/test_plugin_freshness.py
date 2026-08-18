"""An installed plugin can be nine days stale and nothing says so.

Measured on 2026-08-18. A fix to `plugins/herdr.py` was merged, deployed, and
the services restarted -- and the box kept executing the copy installed on
9 August. `pip install` updates HivePilot; it does not touch
`$XDG_DATA_HOME/hivepilot/plugins/*.py`, which is where the runtime actually
loads plugins from.

Nothing in `plugin_installer` compares an installed file to its source: the
words `sha256`, `hash` and `stale` appear once between them, in a comment.
Two runs were spent debugging a defect that had already been fixed.

Same family as every other silence this codebase keeps producing: nothing
lied, and nothing told the truth either. The cure is the one that has worked
elsewhere -- make the value carry its provenance, and make a mismatch
*visible* rather than inferable.

Deliberately a comparison, not a fetch: the network call is the caller's, so
this is testable offline and a health surface can decide how often to pay for
it. And an unreachable source reports UNKNOWN, never `current` -- "I could not
check" must not read as "it is fine".
"""

from __future__ import annotations

import hashlib

import pytest

from hivepilot.services.plugin_freshness import Freshness, compare_plugin


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TestItDetectsTheRealCase:
    def test_a_stale_install_is_reported_stale(self):
        """The measured case: the box's copy differs from the source."""
        result = compare_plugin(
            name="herdr",
            installed_text="# the 9 August copy\n",
            source_text="# with the pane-id fix\n",
        )

        assert result.status is Freshness.STALE
        assert result.name == "herdr"

    def test_an_identical_install_is_current(self):
        text = "# same bytes\n"

        assert compare_plugin(name="herdr", installed_text=text, source_text=text).status is (
            Freshness.CURRENT
        )

    def test_the_comparison_is_over_bytes_not_length(self):
        """Two files of equal length that differ must not read as current --
        a one-character fix is exactly the shape this exists to catch."""
        a, b = "# aaaa\n", "# aaab\n"

        assert compare_plugin(name="x", installed_text=a, source_text=b).status is Freshness.STALE


class TestItRefusesToGuess:
    def test_an_unreachable_source_is_unknown_not_current(self):
        """ "I could not check" must never render as "it is fine" -- that is
        the whole failure mode being corrected."""
        result = compare_plugin(name="herdr", installed_text="# something\n", source_text=None)

        assert result.status is Freshness.UNKNOWN
        assert result.detail

    def test_a_missing_install_is_reported_as_absent(self):
        """Absent and stale are different actions: install versus update."""
        result = compare_plugin(name="herdr", installed_text=None, source_text="# src\n")

        assert result.status is Freshness.ABSENT

    def test_neither_side_available_is_unknown(self):
        assert compare_plugin(name="x", installed_text=None, source_text=None).status is (
            Freshness.UNKNOWN
        )


class TestWhatItReportsBack:
    def test_it_carries_both_digests_so_a_human_can_verify(self):
        result = compare_plugin(name="herdr", installed_text="a", source_text="b")

        assert result.installed_sha == _sha("a")
        assert result.source_sha == _sha("b")

    def test_an_unknown_result_carries_no_invented_digest(self):
        result = compare_plugin(name="herdr", installed_text="a", source_text=None)

        assert result.source_sha is None

    @pytest.mark.parametrize(
        "status,expected",
        [(Freshness.STALE, True), (Freshness.ABSENT, True), (Freshness.CURRENT, False)],
    )
    def test_needs_action_is_explicit(self, status, expected):
        """A caller should not have to reason about which statuses mean 'do
        something'. UNKNOWN is deliberately NOT actionable: it means the check
        failed, not that the plugin is wrong."""
        assert Freshness.needs_action(status) is expected

    def test_unknown_is_not_actionable(self):
        assert Freshness.needs_action(Freshness.UNKNOWN) is False
