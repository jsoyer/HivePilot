"""Is the plugin the runtime loads the plugin we shipped?

Measured on 2026-08-18: a fix to `plugins/herdr.py` was merged, deployed, and
the services restarted — and the box kept executing the copy installed on
9 August. `pip install` updates HivePilot; it does not touch
`$XDG_DATA_HOME/hivepilot/plugins/*.py`, which is where the runtime actually
loads plugins from. Two runs were spent debugging a defect that had already
been fixed.

Nothing compared the two: `sha256`, `hash` and `stale` appear once between
`plugin_installer` and `plugins.py`, in a comment. So a nine-day-old plugin
looked exactly like a current one — the same silence this codebase keeps
producing, and the same cure: make the value carry its provenance, and make a
mismatch visible rather than inferable.

This module compares; it does not fetch. The network call belongs to the
caller, so a health surface decides how often to pay for it, and this stays
testable offline. An unreachable source reports `UNKNOWN` — "I could not
check" must never render as "it is fine", which is the exact failure being
corrected.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


class Freshness(str, Enum):
    #: Installed bytes match the source.
    CURRENT = "current"
    #: Installed, but different from the source — `plugins install <name>`.
    STALE = "stale"
    #: Declared/expected but no file on disk — install it.
    ABSENT = "absent"
    #: The comparison could not be made. NOT a verdict about the plugin.
    UNKNOWN = "unknown"

    @staticmethod
    def needs_action(status: "Freshness") -> bool:
        """Whether an operator has something to do.

        `UNKNOWN` is deliberately excluded. It means the check failed, not that
        the plugin is wrong, and treating it as actionable would train people
        to ignore the one status that means "look at me".
        """
        return status in (Freshness.STALE, Freshness.ABSENT)


@dataclass(frozen=True)
class PluginFreshness:
    name: str
    status: Freshness
    installed_sha: str | None
    source_sha: str | None
    detail: str = ""


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def compare_plugin(
    *, name: str, installed_text: str | None, source_text: str | None
) -> PluginFreshness:
    """Compare an installed plugin against its source.

    Both digests travel back so a human can verify the claim by hand rather
    than trusting this function — and an `UNKNOWN` result carries no invented
    digest for the side it could not read.
    """
    installed_sha = _sha(installed_text)
    source_sha = _sha(source_text)

    if source_text is None:
        return PluginFreshness(
            name=name,
            status=Freshness.UNKNOWN,
            installed_sha=installed_sha,
            source_sha=None,
            detail="the source could not be read; this is not a verdict about the plugin",
        )
    if installed_text is None:
        return PluginFreshness(
            name=name,
            status=Freshness.ABSENT,
            installed_sha=None,
            source_sha=source_sha,
            detail="no installed copy on disk",
        )

    if installed_sha == source_sha:
        return PluginFreshness(
            name=name,
            status=Freshness.CURRENT,
            installed_sha=installed_sha,
            source_sha=source_sha,
        )
    return PluginFreshness(
        name=name,
        status=Freshness.STALE,
        installed_sha=installed_sha,
        source_sha=source_sha,
        detail=f"installed copy differs from source; run `hivepilot plugins install {name}`",
    )
