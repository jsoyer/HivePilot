"""Historical cost backfill (usage-capture-modelusage fix).

Before this fix, `steps.model` frequently persisted a bare CLI alias
(``sonnet``/``opus``/``haiku``) rather than a canonical model id, and
`steps.cost_usd` was left ``NULL`` -- or, in some observed rows, an
impossible ``0.0`` alongside real, nonzero token counts -- because the top-
level ``usage``/``model`` envelope fields the runner read were empty (see
``claude_runner._parse_usage_envelope``'s history).

**Honesty boundary**: the raw CLI JSON envelope was NEVER persisted for a
successful step. `steps.detail` is populated only for a FAILED step (``str
(exc)`` -- see ``orchestrator._record_step_success``, which passes no
``detail`` on the success path at all). That means the actual
`cache_read_tokens`/`cache_creation_tokens` for every historical row are
genuinely gone -- there is nothing left to recover them from, and this
module makes NO attempt to fabricate them. What this backfill CAN honestly
recover is a cost estimate from the base `input_tokens`/`output_tokens`
already on each row, using the price map now extended to cover bare CLI
aliases and the `-4-5` model generation (see `pricing.DEFAULT_PRICE_MAP`).
That estimate is, by construction, a LOWER BOUND for any row that actually
had cache activity (its cache token cost cannot be added back) -- never an
exact recomputation, and never silently claimed to be one.

This is offered as an explicit, opt-in command (`hivepilot costs backfill`)
-- never an automatic migration side effect -- and defaults to a dry run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hivepilot.services import db, pricing, state_service


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Outcome of one `backfill_unpriced_costs()` call.

    ``scanned`` is every candidate row considered (has a model, has at least
    one of input/output tokens, and currently has no usable self-reported
    cost). ``updated`` is how many of those were priced by this pass (and,
    when ``apply=True``, actually written). ``still_unpriced`` is the
    remainder -- rows this backfill genuinely could not price (model absent
    from the effective price map even after the expansion) -- reported
    honestly rather than silently dropped from the total.
    """

    scanned: int
    updated: int
    still_unpriced: int
    updated_ids: list[int] = field(default_factory=list)


# A step's cost_usd is a backfill CANDIDATE when it's NULL, or when it's
# exactly 0.0 -- a real production symptom of the bug this fix addresses
# (a paid model cannot genuinely cost $0.0 for a real, nonzero token count;
# see the module docstring). A row with any other self-reported value is
# never touched -- that's a real, trusted number.
_CANDIDATE_WHERE = (
    "model IS NOT NULL "
    "AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL) "
    "AND (cost_usd IS NULL OR cost_usd = 0)"
)


def backfill_unpriced_costs(*, apply: bool = False) -> BackfillResult:
    """Recompute `cost_usd` for historical steps from the model + token
    counts already on each row (see module docstring for the honesty
    boundary: cache tokens for pre-fix rows are NOT recoverable).

    ``apply=False`` (the default) is a dry run: computes and returns what
    WOULD change without writing anything. Only ``apply=True`` writes to
    the database -- this is never invoked as an automatic migration side
    effect (see `state_service.init_db`, which never calls this).
    """
    state_service.init_db()
    with db.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                db.ph(
                    "SELECT id, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_creation_tokens "
                    "FROM steps "
                    f"WHERE {_CANDIDATE_WHERE}"
                )
            ).fetchall()
        ]

    updates: list[tuple[float, int]] = []
    still_unpriced = 0
    for row in rows:
        estimated = pricing.estimate_cost(
            row.get("model"),
            row.get("input_tokens") or 0,
            row.get("output_tokens") or 0,
            cache_read_tokens=row.get("cache_read_tokens") or 0,
            cache_creation_tokens=row.get("cache_creation_tokens") or 0,
        )
        if estimated is None:
            still_unpriced += 1
            continue
        updates.append((estimated, row["id"]))

    if apply and updates:
        with db.connect() as conn:
            for cost, step_id in updates:
                conn.execute(db.ph("UPDATE steps SET cost_usd=? WHERE id=?"), (cost, step_id))

    return BackfillResult(
        scanned=len(rows),
        updated=len(updates),
        still_unpriced=still_unpriced,
        updated_ids=[step_id for _, step_id in updates],
    )


__all__: list[str] = ["BackfillResult", "backfill_unpriced_costs"]
