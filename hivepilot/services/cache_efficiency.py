"""Find the steps that create prompt cache and never read it back.

Measured in production across 132 model steps and 43 M prompt-side tokens:
85.0% overall hit rate. Healthy — and it hid the problem completely.

One step sat at 49.8%, with the same shape on every run:

    ceo intake   cache_creation ~43 000     cache_read ~16 000

A fresh prefix each time. Creation is billed at 1.25x base input, a read at
0.1x, so that is full price paid nineteen times and never amortised.

The aggregate rate cannot show this. A high-volume healthy step drowns a
low-volume pathological one, and the number that looks fine is the reason
nobody looks further. So the useful measure is a per-step ratio rather than
a global rate:

    amortisation = cache_read / cache_creation

Below 1.0 means the step has created more cache than it has ever read back.
That is a prompt-structure problem -- something variable sitting ahead of
something stable, so nothing after it can be cached -- and no proxy fixes
it; the prompt has to be reordered.

**This module only reports.** Which prompt to reorder is the deployment's
decision. Finding the step that needs it is the engine's job, and it is one
nobody can do by eye against a healthy-looking 85%.
"""

from __future__ import annotations

from typing import Any

from hivepilot.services import db, state_service

__all__ = ["cache_summary"]

#: A step needs to have run at least this many times before its amortisation
#: means anything. The first run of anything has nothing to reuse yet, so
#: flagging it would report a cold start as a defect.
_MIN_RUNS = 2

#: Below this, the step created more cache than it ever read back.
_AMORTISATION_FLOOR = 1.0


def cache_summary(*, tenant: str | None = None) -> dict[str, Any]:
    """Prompt-cache efficiency: the global rate, and the steps failing it.

    `hit_rate` is None rather than 0.0 when there is nothing to measure: a
    rate of zero reads as "the cache never works", which is a different and
    much louder claim than "no model step has run yet".
    """
    state_service.init_db()
    with db.connect() as conn:
        # Per ROW, not grouped. The first version summed each step's tokens
        # across its runs and found nothing on real data: `ceo intake` is
        # pathological on nine runs out of ten, and a single outlier that
        # read 326 696 tokens lifted the *sum* over the floor and un-flagged
        # it. Summing hid the step exactly the way the global 85% hit rate
        # hid it -- the same mistake, one level down.
        rows = conn.execute(
            "SELECT step, COALESCE(input_tokens, 0) AS inp, "
            "COALESCE(cache_read_tokens, 0) AS reads, "
            "COALESCE(cache_creation_tokens, 0) AS creates "
            # A step with no model had no prompt and so no cache to hit;
            # counting it would dilute the rate with rows that never could.
            "FROM steps WHERE model IS NOT NULL"
        ).fetchall()

    total_in = total_read = total_create = 0
    step_count = 0
    per_step: dict[str, list[tuple[int, int]]] = {}

    for row in rows:
        reads = int(row["reads"])
        creates = int(row["creates"])
        step_count += 1
        total_in += int(row["inp"])
        total_read += reads
        total_create += creates
        per_step.setdefault(str(row["step"]), []).append((reads, creates))

    unamortised: list[dict[str, Any]] = []
    for step, samples in per_step.items():
        # Only runs that created cache can say anything about amortising it.
        ratios = sorted(r / c for r, c in samples if c > 0)
        if len(ratios) < _MIN_RUNS:
            continue
        median = (
            ratios[len(ratios) // 2]
            if len(ratios) % 2
            else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
        )
        if median < _AMORTISATION_FLOOR:
            unamortised.append(
                {
                    "step": step,
                    "runs": len(ratios),
                    "cache_read": sum(r for r, _ in samples),
                    "cache_creation": sum(c for _, c in samples),
                    "amortisation": median,
                }
            )

    prompt_side = total_in + total_read + total_create
    return {
        "steps": step_count,
        "hit_rate": (total_read / prompt_side) if prompt_side else None,
        "cache_read": total_read,
        "cache_creation": total_create,
        # Worst first: the point of the list is where to look, and an
        # operator reads the top of it.
        "unamortised": sorted(unamortised, key=lambda e: e["amortisation"]),
    }
