"""Two measurements of the same money, kept apart.

Cost reaches this system by two independent paths:

    envelope -- `steps.cost_usd`, self-reported by the agent in `--print`
                mode's JSON, one figure per step;
    otel     -- `claude_code.cost.usage`, exported per API request.

They measure THE SAME SPEND. Adding them double-counts it, so this module
offers no combined total at all -- not as a default, not as an option. A
dashboard showing one confident number assembled from both is worse than one
showing neither, because the second at least invites the question.

Why a panel and not a number, measured on the box:

    envelope  404.51 USD   357 steps    2026-07-26 -> 2026-08-19
    otel      169.13 USD  1929 rows     2026-08-10 -> 2026-08-19

A 2.4x gap that looks like catastrophic telemetry loss and is nothing of the
kind: OTel export landed on the 10th, so the two cover different windows. Put
those totals side by side without their coverage and somebody spends a day
hunting money that was never missing.

So every basis carries its window, the report says outright whether the two are
comparable, and the divergence is computed ONLY when they are.
"""

from __future__ import annotations

from typing import Any

#: Below this, a divergence percentage is arithmetic noise rather than a
#: finding -- and dividing by a total that is effectively zero produces a
#: number that says more about floating point than about spend.
_MIN_BASE_USD = 0.01


def compare_cost_bases(
    *,
    envelope: dict[str, Any] | None,
    otel: dict[str, Any] | None,
) -> dict[str, Any]:
    """Report both bases side by side, and whether comparing them is valid.

    `None` for a basis means NOT MEASURED -- the exporter never ran, or no
    step ever reported. Zero dollars is a different statement entirely: a
    period in which nothing was spent. Collapsing the two makes a dead
    exporter look like a free week.

    `comparable` is False whenever the two windows differ or either is
    unknown, and `divergence_pct` is `None` in exactly those cases. A ratio
    across different periods is a number that means nothing, and printing it
    invites precisely the wrong conclusion.
    """
    comparable = _same_window(envelope, otel)
    return {
        "envelope": envelope,
        "otel": otel,
        # Deliberately no combined total. See the module docstring: these are
        # two readings of one spend, not two components of it.
        "comparable": comparable,
        "divergence_pct": _divergence(envelope, otel) if comparable else None,
    }


def _same_window(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a or not b:
        return False
    keys = ("first", "last")
    if any(not a.get(k) or not b.get(k) for k in keys):
        # A total whose period nobody can establish cannot be compared, and
        # saying it can is how the gap gets misread.
        return False
    return all(a.get(k) == b.get(k) for k in keys)


def _divergence(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    """How far the second reading falls from the first, as a percentage.

    Zero is a FINDING -- the two paths agree. `None` means the question could
    not be asked, which is why the two are never conflated.
    """
    if not a or not b:
        return None
    base = a.get("total_usd")
    other = b.get("total_usd")
    if not isinstance(base, (int, float)) or not isinstance(other, (int, float)):
        return None
    if abs(base) < _MIN_BASE_USD:
        return None
    return round(abs(base - other) / base * 100, 2)
