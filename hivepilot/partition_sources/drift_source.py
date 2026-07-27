"""`DriftSource` -- the `drift` built-in partition source (propose -> ratify
-> dispatch PRD, Sprint 1, spec section 4): a single `drift_scans` row, as
proposer-fetchable context.

No single-row-by-id accessor exists on `state_service` for `drift_scans`
(only `get_recent_drift_scans`/`get_drift_baseline`, both project-scoped,
not id-scoped) -- this module queries the table directly via
`hivepilot.services.db`, mirroring `verdict_source.py`'s own rationale for
doing the same (see that module's docstring)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hivepilot.services import db, state_service

if TYPE_CHECKING:
    from hivepilot.partition_sources import SourceDocument


class DriftSourceError(RuntimeError):
    """Raised when *ref* is blank, isn't an integer drift-scan id, or
    doesn't resolve to an existing `drift_scans` row."""


class DriftSource:
    """`PartitionSource` reading a single `drift_scans` row by id."""

    name = "drift"

    def fetch(self, ref: str) -> "SourceDocument":
        from hivepilot.partition_sources import SourceDocument, compute_digest

        cleaned = (ref or "").strip()
        if not cleaned:
            raise DriftSourceError("drift source requires a non-blank drift-scan id")
        try:
            drift_id = int(cleaned)
        except ValueError as exc:
            raise DriftSourceError(
                f"drift source ref must be an integer drift-scan id, got {ref!r}"
            ) from exc

        state_service.init_db()
        with db.connect() as conn:
            row = conn.execute(
                db.ph("SELECT * FROM drift_scans WHERE id=?"), (drift_id,)
            ).fetchone()
        if row is None:
            raise DriftSourceError(f"no drift scan found with id {drift_id}")
        scan = dict(row)

        lines = [
            f"Drift scan #{drift_id} -- project={scan.get('project')!r} "
            f"runner={scan.get('runner')!r} status={scan.get('status')!r} "
            f"drifted={bool(scan.get('drifted'))}"
        ]
        if scan.get("to_add") is not None or scan.get("to_change") is not None:
            lines.append(
                f"to_add={scan.get('to_add')} to_change={scan.get('to_change')} "
                f"to_destroy={scan.get('to_destroy')}"
            )
        if scan.get("detail"):
            lines.append(f"detail: {scan['detail']}")
        content = "\n".join(lines)

        return SourceDocument(
            kind="drift",
            ref=cleaned,
            digest=compute_digest(content),
            content=content,
            metadata={"drift_id": drift_id, "drifted": bool(scan.get("drifted"))},
        )
