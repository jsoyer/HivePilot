"""Which failed runs' workspaces stay open, and which get closed.

A run that succeeded leaves nothing worth opening. A run that FAILED is the one
an operator wants: the agent's scrollback, its exact environment, the ability
to re-run the failing command where it failed. Closing those was the wrong
default, and it was defended with an accumulation cost that had been asserted
rather than measured.

Measured afterwards on the box: 7-10 failed runs a day on busy days, 233 in a
month. The cost is real -- which argues for a BOUND, not for closing. At N=5
that leaves roughly half a day of failures open, which is the window in which
anyone actually looks.

Committing work-in-progress on failure is a complement, not a substitute: it
preserves FILES, and nothing about the terminal, the environment, or the
ability to poke at the tree in place.

Pure functions on purpose. This module decides what gets destroyed, and that is
not a decision anyone should have to trace through a call graph to audit.
"""

from __future__ import annotations

import re

#: A workspace label this engine minted: "<project> run <id>".
#:
#: Anchored and requiring the literal `run` marker, because this module's
#: output is a list of things to CLOSE. An operator's own workspace, or one
#: belonging to another tool, must never be closed by a bound it never opted
#: into -- so anything that does not positively match is not ours.
_OURS = re.compile(r"^.*\brun\s+(\d+)$")


def run_id_from_label(label: str) -> int | None:
    """The run id in a label we minted, or None when the label is not ours.

    None is the safe answer: the caller drops the workspace from consideration
    entirely rather than guessing an ordering for it. An unorderable entry that
    were treated as "oldest" would be closed first, and the one thing worse
    than keeping a stranger's workspace is closing it.
    """
    match = _OURS.match(label.strip()) if label else None
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the pattern guarantees digits
        return None


def workspaces_to_close(kept: list[tuple[str, int]], *, keep: int) -> list[str]:
    """Ids to close, given every retained workspace and how many to keep.

    Ordered by RUN ID, never by discovery order: `herdr workspace list` returns
    whatever order it likes, and pruning by that would retain an arbitrary set
    rather than the most recent one.

    `keep <= 0` closes everything. Zero is the default and must reproduce
    today's behaviour exactly; a negative value is a config typo and is read as
    "retain none", never as "unlimited" -- a bound that a typo can silently
    remove is not a bound.
    """
    if keep <= 0:
        return [workspace_id for workspace_id, _ in kept]
    newest_first = sorted(kept, key=lambda item: item[1], reverse=True)
    # Returned OLDEST first. The set is what matters, but a deterministic order
    # makes the log read the way the action happens.
    return [workspace_id for workspace_id, _ in reversed(newest_first[keep:])]
