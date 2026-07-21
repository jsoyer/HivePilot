"""Unit tests for `hivepilot.banner` -- the bee ASCII-art wizard banner.

Kept deliberately small: the only real "correctness" property of a piece of
decorative ASCII art is that it renders without error and stays within an
80-column terminal (no truncation/wrapping) -- there is no behavior to
assert beyond that.
"""

from __future__ import annotations

from rich.console import Console

from hivepilot.banner import BANNER_ART, render_banner


def test_banner_art_is_non_empty() -> None:
    assert BANNER_ART.strip() != ""
    assert len(BANNER_ART.splitlines()) >= 5


def test_banner_art_reads_as_a_bee() -> None:
    # Stable markers of the side-profile bumblebee art: "HIVE" spelled out
    # in the abdomen and a stinger -- not a hardcoded full-string match, so
    # the art can still be tweaked without brittle test churn.
    assert "H I V E" in BANNER_ART
    assert "===>" in BANNER_ART


def test_banner_art_lines_fit_80_columns() -> None:
    for line in BANNER_ART.splitlines():
        assert len(line) <= 80, f"banner line too wide ({len(line)} cols): {line!r}"


def test_banner_art_has_no_tab_characters() -> None:
    assert "\t" not in BANNER_ART


def test_render_banner_runs_without_error() -> None:
    console = Console(record=True, width=100)
    render_banner(console)
    output = console.export_text()
    assert "HivePilot" in output


def test_render_banner_with_custom_subtitle() -> None:
    console = Console(record=True, width=100)
    render_banner(console, subtitle="Guided setup")
    output = console.export_text()
    assert "Guided setup" in output


def test_render_banner_default_tagline() -> None:
    console = Console(record=True, width=100)
    render_banner(console)
    output = console.export_text()
    assert "Buzz your agents into formation." in output
