"""Find the steps that create prompt cache and never read it back.

Measured in production across 132 model steps and 43 M prompt-side tokens:
85.0% overall hit rate. Healthy — and it hid the per-step picture completely.
A high-volume healthy step drowns a low-volume pathological one, and the
number that looks fine is the reason nobody looks further. So the useful
measure is a per-step ratio rather than a global rate:

    amortisation = cache_read / cache_creation

Below 1.0 means the step created more cache than it read back.

**A low ratio is not by itself a defect**, and the first version of this
module said it was. `ceo intake` sat at 0.34 on every run and was reported
as a prompt-structure problem; production data says it is nothing of the
kind:

    ceo intake      read 15 268   created 45 517   -> 0.335
    implementation  read 50 465 734  created 334 906  -> 150.7

The 15 268 is read back *within* the dispatch and is byte-identical three
runs apart, so that prefix is stable and is being cached exactly as
intended. What separates 0.34 from 150.7 is not prompt quality but how many
turns the step runs: `implementation` reads its prefix back on each of many
turns, while `ceo intake` is a short first stage that writes its prompt once
and finishes. A short step therefore sits below 1.0 *by construction*,
however well its prompt is ordered — and telling an operator to reorder it
sends them to fix something that is already correct.

So `turns` decides which of the two a low ratio is:

- **unamortised** — below the floor with turns to spare. The prefix could
  have been read back on each of them and was not: something variable is
  sitting ahead of something stable. Actionable; the prompt has to be
  reordered, and no proxy fixes it.
- **single_pass** — below the floor with too few turns for the ratio to
  mean anything. Not actionable, and not a defect.
- **unclassified** — below the floor, turn count unknown (every row written
  before the column existed). Counted, never resolved by guess: "we cannot
  tell" is a different statement from "this is fine", and neither one is
  "this is broken".

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

#: Fewer turns than this and the step had at most one opportunity to read
#: back what it wrote, so a ratio under 1.0 says nothing about how the prompt
#: is ordered. Two turns can at best break even (turn 1 writes the prefix,
#: turn 2 reads it); it takes a third before a well-ordered prompt should
#: clear the floor comfortably.
_MIN_TURNS_FOR_REUSE = 3


def _median(values: list[float]) -> float:
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


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
            "COALESCE(cache_creation_tokens, 0) AS creates, "
            # NOT coalesced: an absent turn count is the one thing that must
            # stay distinguishable from a real one.
            "turns AS turns "
            # A step with no model had no prompt and so no cache to hit;
            # counting it would dilute the rate with rows that never could.
            "FROM steps WHERE model IS NOT NULL"
        ).fetchall()

    total_in = total_read = total_create = 0
    step_count = 0
    per_step: dict[str, list[tuple[int, int, int | None]]] = {}

    for row in rows:
        reads = int(row["reads"])
        creates = int(row["creates"])
        raw_turns = row["turns"]
        step_count += 1
        total_in += int(row["inp"])
        total_read += reads
        total_create += creates
        per_step.setdefault(str(row["step"]), []).append(
            (reads, creates, int(raw_turns) if raw_turns is not None else None)
        )

    unamortised: list[dict[str, Any]] = []
    single_pass: list[dict[str, Any]] = []
    unclassified = 0

    for step, samples in per_step.items():
        # Only runs that created cache can say anything about amortising it.
        scoring = [(r, c, t) for r, c, t in samples if c > 0]
        if len(scoring) < _MIN_RUNS:
            continue
        median_ratio = _median(sorted(r / c for r, c, _ in scoring))
        if median_ratio >= _AMORTISATION_FLOOR:
            continue

        known_turns = sorted(float(t) for _, _, t in scoring if t is not None)
        if not known_turns:
            # Below the floor and no way to say why. Reported as an open
            # question rather than assigned to whichever bucket happens to
            # suit the reader.
            unclassified += 1
            continue

        median_turns = int(_median(known_turns))
        entry = {
            "step": step,
            "runs": len(scoring),
            "cache_read": sum(r for r, _, _ in samples),
            "cache_creation": sum(c for _, c, _ in samples),
            "amortisation": median_ratio,
            "turns": median_turns,
        }
        if median_turns < _MIN_TURNS_FOR_REUSE:
            single_pass.append(entry)
        else:
            unamortised.append(entry)

    prompt_side = total_in + total_read + total_create
    return {
        "steps": step_count,
        "hit_rate": (total_read / prompt_side) if prompt_side else None,
        "cache_read": total_read,
        "cache_creation": total_create,
        # Worst first: the point of the list is where to look, and an
        # operator reads the top of it.
        "unamortised": sorted(unamortised, key=lambda e: e["amortisation"]),
        # Reported so the panel can say "we looked and there is nothing to
        # do here", which is not the same as omitting the step entirely.
        "single_pass": sorted(single_pass, key=lambda e: e["amortisation"]),
        "unclassified": unclassified,
    }
