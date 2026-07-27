"""`VerdictSource` -- the `verdict` built-in partition source (propose ->
ratify -> dispatch PRD, Sprint 1, spec section 4): a single `verdicts` row,
as proposer-fetchable context.

No single-row-by-id accessor exists on `state_service` for `verdicts`
(only `list_verdicts`/`list_recent_verdicts`, both list-shaped) -- this
module queries the table directly via `hivepilot.services.db`, the same
portable low-level DB helper `state_service` itself is built on, mirroring
the existing pattern of `hivepilot.services.{analytics_service,
autopilot_queue,headroom_metrics,memory_service,retry_service}` importing
`db` directly for their own concern-specific queries rather than growing
`state_service`'s surface for every caller (S1's file boundary declares
`state_service.py` read-only for this sprint).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hivepilot.services import db, state_service

if TYPE_CHECKING:
    from hivepilot.partition_sources import SourceDocument


class VerdictSourceError(RuntimeError):
    """Raised when *ref* is blank, isn't an integer verdict id, or doesn't
    resolve to an existing `verdicts` row."""


class VerdictSource:
    """`PartitionSource` reading a single `verdicts` row by id."""

    name = "verdict"

    def fetch(self, ref: str) -> "SourceDocument":
        from hivepilot.partition_sources import SourceDocument, compute_digest

        cleaned = (ref or "").strip()
        if not cleaned:
            raise VerdictSourceError("verdict source requires a non-blank verdict id")
        try:
            verdict_id = int(cleaned)
        except ValueError as exc:
            raise VerdictSourceError(
                f"verdict source ref must be an integer verdict id, got {ref!r}"
            ) from exc

        state_service.init_db()
        with db.connect() as conn:
            row = conn.execute(db.ph("SELECT * FROM verdicts WHERE id=?"), (verdict_id,)).fetchone()
        if row is None:
            raise VerdictSourceError(f"no verdict found with id {verdict_id}")
        verdict = dict(row)

        lines = [
            f"Verdict #{verdict_id} -- role={verdict.get('role')!r} "
            f"kind={verdict.get('kind')!r} decision={verdict.get('decision')!r}"
        ]
        if verdict.get("confidence") is not None:
            lines.append(f"confidence: {verdict['confidence']}")
        if verdict.get("summary"):
            lines.append(f"summary: {verdict['summary']}")
        content = "\n".join(lines)

        return SourceDocument(
            kind="verdict",
            ref=cleaned,
            digest=compute_digest(content),
            content=content,
            metadata={"verdict_id": verdict_id, "decision": verdict.get("decision")},
        )
