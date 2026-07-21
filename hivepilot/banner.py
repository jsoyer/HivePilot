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

# A side-profile bumblebee flying right: motion/buzz lines trailing on the
# left, a rounded head and wings, "H I V E" lettered into the abdomen, and a
# stinger on the right. Every line is well under 80 columns and contains no
# tab characters (see tests/test_banner.py). Leading whitespace is
# load-bearing -- preserve it exactly when editing.
BANNER_ART = (
    r"""
    z            .-.       .-.
     z          /   \     /   \
      z        (     \   /     )
   ~ ~ ~ ~ ~    \     \ /     /
       __        '-.   V   .-'
      /  \___       \  |  /
     ( o      `-.____.--+--.._____
      \  \_        (             `-.___
       '-. `-.___.-'  H I V E         `.===>---
          `-.__      (              _.-'
               `-.____`--..______..-'
                       `--------'
"""
).strip("\n")

# Content-based styling (never per-character -- misalignment risk isn't
# worth it for decorative art). A line is classified by what it contains
# rather than its index, since the buzz/motion trails and the "HIVE" stripe
# are interleaved with the plain body lines rather than grouped in a block:
#   - "z" / "~"  -> the buzz trail and wind lines trailing off to the left
#   - "H I V E"  -> the signature stripe lettered into the abdomen
#   - anything else -> the bee's body (head, wings, outline, stinger)
_MOTION_STYLE = "dim cyan"
_STRIPE_STYLE = "bold black on bright_yellow"
_BODY_STYLE = "bold bright_yellow"

_DEFAULT_TAGLINE = "Buzz your agents into formation."


def _line_style(line: str) -> str:
    """Pick a rich style for one banner line based on its role in the bee."""
    if "H I V E" in line:
        return _STRIPE_STYLE
    if "z" in line or "~" in line:
        return _MOTION_STYLE
    return _BODY_STYLE


def render_banner(console: "Console", subtitle: str | None = None) -> None:
    """Print the colored bee banner, the "HivePilot" wordmark, and a
    tagline (default: *"Buzz your agents into formation."*, or *subtitle*
    when given -- e.g. a closing message at the end of the wizard)."""
    from rich.text import Text

    art = Text()
    for line in BANNER_ART.splitlines():
        art.append(line + "\n", style=_line_style(line))
    console.print(art)

    console.print(Text("───────  HivePilot  ───────", style="bright_yellow"), justify="center")
    console.print(Text(subtitle or _DEFAULT_TAGLINE, style="dim italic"), justify="center")
