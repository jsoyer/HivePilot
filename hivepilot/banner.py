"""HivePilot's bee mascot ASCII-art banner, used by `hivepilot setup`.

Kept intentionally dependency-light: only `rich` (already a hard dependency
across the CLI) and only the `Text`/`Console` primitives. `BANNER_ART` is a
plain, uncolored string (so it can be measured/tested independently of any
terminal styling) -- `render_banner` is what applies color.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

# A front-facing bumblebee: antennae, a head with two eyes flanked by wings,
# and a striped, stingered abdomen. Every line is well under 80 columns and
# contains no tab characters (see tests/test_banner.py). Leading whitespace
# is load-bearing -- preserve it exactly when editing.
BANNER_ART = (
    r"""
                \   /
              .._\ /_..
   .-"-.      / o   o \      .-"-.
  /     \____/    ^    \____/     \
  \      ` `  \  '-'  /  ` `      /
   '-.._____.-'\_____/'-.._____.-'
             .-'''''''-.
            / ::::::::: \
           |:::::::::::::|
           |:::::::::::::|
            \ ::::::::: /
             '-._____.-'
                \  |
                 \_|
"""
).strip("\n")

# Line-index-based styling (never per-character -- misalignment risk isn't
# worth it for decorative art). Indices below refer to `BANNER_ART.splitlines()`:
#   0-1   antennae
#   2-5   head + wings
#   6-11  striped abdomen (the `:::` rows get the black-on-yellow stripe look;
#         the cap/base curves stay a plain bold yellow)
#   12-13 stinger
_ANTENNA_STYLE = "dim yellow"
_HEAD_STYLE = "bright_yellow"
_ABDOMEN_STYLE = "bold bright_yellow"
_STRIPE_STYLE = "bold black on bright_yellow"
_STINGER_STYLE = "dim yellow"

_DEFAULT_TAGLINE = "Buzz your agents into formation."


def _line_style(index: int, line: str) -> str:
    """Pick a rich style for one banner line based on its role in the bee."""
    if index <= 1:
        return _ANTENNA_STYLE
    if index <= 5:
        return _HEAD_STYLE
    if index <= 11:
        return _STRIPE_STYLE if ":" in line else _ABDOMEN_STYLE
    return _STINGER_STYLE


def render_banner(console: "Console", subtitle: str | None = None) -> None:
    """Print the colored bee banner, the "HivePilot" wordmark, and a
    tagline (default: *"Buzz your agents into formation."*, or *subtitle*
    when given -- e.g. a closing message at the end of the wizard)."""
    from rich.text import Text

    art = Text()
    for index, line in enumerate(BANNER_ART.splitlines()):
        art.append(line + "\n", style=_line_style(index, line))
    console.print(art)

    console.print(Text("───────  HivePilot  ───────", style="bright_yellow"), justify="center")
    console.print(Text(subtitle or _DEFAULT_TAGLINE, style="dim italic"), justify="center")
