"""Summarise recorded context truncations for a dashboard.

Run 639 is why this exists. `cap` mode kept the TAIL of the joined prior
context, so ~90% of the run vanished -- including both verdicts the release
gate needed -- and the gate then refused a release on a clearance that HAD been
given. It took a week to find, because the only trace was a `logger.warning` in
a file nobody opens until something is already wrong.

Pure functions on rows. The recording lives in `state_service` and the query in
the API; keeping the arithmetic here means the counting rules below can be
argued with directly, without a database.

Two of those rules carry the weight:

    an empty input means "nothing was RECORDED", never "nothing was
    truncated". The first is a fact about whether anyone was writing it down;
    a dashboard that shows a confident zero for a table nobody writes to is
    the exact shape of run 639;

    the WORST single stage is reported, never an average. The purpose of this
    figure is to name the one stage whose output is blowing the budget, and an
    average is precisely the statistic that hides it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _number(row: dict[str, Any], key: str) -> int | None:
    """An int, or None. A missing value is NOT zero -- zero dropped characters
    is a measurement, absent is an unwritten field, and folding the second into
    the first understates every total it touches."""
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def summarise_truncations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, totals and the worst offender, from recorded truncation rows.

    `by_basis` splits on where the budget came from: `derived` from the model's
    real context window, `fallback` from a configured constant nobody
    re-checked. A row written before the basis was recorded is `unknown` --
    not `fallback`, because an unclassifiable row is a gap and calling it
    anything else is a guess.

    `worst_role` names whose output to go and read. A row with no role never
    wins it: a NULL sorts as something, and letting it become the answer to
    "who should I look at" would send the operator nowhere.
    """
    dropped_total = 0
    worst_stage: int | None = None
    basis: Counter[str] = Counter()
    per_role: Counter[str] = Counter()

    for row in rows:
        dropped = _number(row, "dropped_chars")
        if dropped is not None:
            dropped_total += dropped

        largest = _number(row, "largest_stage_chars")
        if largest is not None:
            worst_stage = largest if worst_stage is None else max(worst_stage, largest)

        raw_basis = row.get("budget_basis")
        basis[raw_basis if isinstance(raw_basis, str) and raw_basis else "unknown"] += 1

        role = row.get("role")
        if isinstance(role, str) and role and dropped is not None:
            per_role[role] += dropped

    return {
        # "recorded", not "truncations": it counts rows that exist, and says
        # nothing about runs nobody wrote down.
        "recorded": len(rows),
        "dropped_chars": dropped_total,
        "worst_stage_chars": worst_stage,
        "worst_role": per_role.most_common(1)[0][0] if per_role else None,
        "by_basis": dict(basis),
    }
