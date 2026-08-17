"""What a capped vault scan actually covered, stated rather than implied.

Measured on the box, 2026-08-17: `noxys-obsidian-vault` holds 2 125 notes and
`plugins/obsidian.py`'s `_MAX_NOTES_SCANNED` is 500. Recall sees 24% of the
corpus and reports nothing about the other 76%.

The cap has already caused one incident. It used to be
`sorted(rglob("*.md"))[:500]` -- ALPHABETICAL -- so the 500 notes it read were
a quarter of the vault chosen by filename, and every note the agents had
produced was structurally excluded. The fix changed the sort KEY to mtime and
left the principle alone: a note of substance written six months ago is still
out of reach, and nothing says so.

That is the run-639 context truncation in a different subsystem. A cut that
records nothing is indistinguishable from no cut at all, and an operator
asking "why did it not remember the ADR" has no way to find out that the ADR
was never eligible.

This module does no I/O and makes no policy. It turns (total, cap) into the
numbers a caller must log, so the cap can be tuned on evidence instead of
taste -- the same treatment already given to the context budget.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanBudget:
    """How much of a corpus a capped scan will actually read.

    Frozen: a caller must not be able to rewrite the numbers it then reports.
    """

    total: int
    scanned: int
    skipped: int
    truncated: bool
    coverage: float


def plan_scan(*, total: int, cap: int) -> ScanBudget:
    """Resolve a scan of *total* notes under *cap*.

    A non-positive *cap* reads NOTHING and says so. Treating it as "no limit"
    would turn a configuration mistake into a silently expensive full scan --
    exactly the wrong direction for a module whose point is that limits must
    be visible.

    `coverage` is reported alongside the raw counts because "skipped 1 625"
    invites a guess while 0.24 states the problem: three quarters of the
    operator's second brain is unreachable. An empty corpus reports full
    coverage rather than dividing by zero -- nothing was missed.
    """
    total = max(0, total)
    if cap <= 0:
        return ScanBudget(
            total=total,
            scanned=0,
            skipped=total,
            truncated=True,
            coverage=0.0 if total else 1.0,
        )

    scanned = min(total, cap)
    skipped = total - scanned
    return ScanBudget(
        total=total,
        scanned=scanned,
        skipped=skipped,
        truncated=skipped > 0,
        coverage=(scanned / total) if total else 1.0,
    )
