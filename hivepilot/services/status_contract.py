"""Derived-status contract (HP-42, Cycle 1 · P1 "Live & Missions").

The single, pure mapping from a raw ``runs.status`` fact to (a) a board
*column* and (b) an *attention zone* — "where should the operator look?".
Nothing here is stored: display status is DERIVED from the status fact at read
time (the Agent-Orchestrator principle), so there is exactly one place the
mapping lives and it can never drift out of sync with a persisted copy.

This is the Python source of truth. `web/src/lib/status-contract.ts` mirrors it
verbatim for the Pollen board; `tests/test_status_contract.py` and
`status-contract.test.ts` pin both sides to the same table, and a drift guard
here pins the failure/success sets to `analytics_service` (the canonical
outcome classifier) so the board and the analytics can never disagree about
what "failed" means.

Attention zones (ordered most → least urgent):
  needs_you  — a human must act: something failed, or a decision is pending
  in_review  — under review right now
  working    — an agent is actively running it
  queued     — accepted, not started yet
  ready      — finished successfully; nothing to do
  other      — paused / cancelled / deferred / unrecognized
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hivepilot.services.analytics_service import _FAILED_STATUSES, _SUCCEEDED_STATUSES

Column = Literal["queued", "running", "waiting_approval", "failed", "done", "other"]
Zone = Literal["needs_you", "in_review", "working", "queued", "ready", "other"]

# --- status buckets -------------------------------------------------------
# `queued`/`waiting_approval` are board concepts the analytics classifier does
# not model (it only cares about terminal OUTCOMES), so they are defined here.
# `failed`/`done` REUSE the analytics sets rather than re-listing them, so the
# board and `canonical_outcome()` can never disagree (guarded by a test).

#: Pre-execution: formal RunStatus.NEW/PLANNED + the "pending" literal
#: `create_run` stores for a require-approval initial run (also RunStatus's
#: legacy alias for NEW).
QUEUED_STATUSES = frozenset({"new", "planned", "pending"})

#: A human decision is pending: RunStatus.APPROVAL/REVIEW + the
#: "awaiting_approval" literal the orchestrator sets at its checkpoint.
WAITING_APPROVAL_STATUSES = frozenset({"approval", "awaiting_approval", "review"})

#: Actively executing.
RUNNING_STATUSES = frozenset({"running"})

#: Terminal failure states — the canonical `analytics_service` set.
FAILED_STATUSES = frozenset(_FAILED_STATUSES)

#: Terminal success states — the canonical `analytics_service` set.
DONE_STATUSES = frozenset(_SUCCEEDED_STATUSES)


def _normalise(status: str | None) -> str:
    return (status or "").strip().lower()


def derive_column(status: str | None) -> Column:
    """Map a raw status to its Kanban column. Anything unrecognized (paused,
    cancelled, deferred, or a genuinely unknown string) lands in ``other`` —
    the board never invents a stronger classification than the fact supports."""
    s = _normalise(status)
    if s in QUEUED_STATUSES:
        return "queued"
    if s in RUNNING_STATUSES:
        return "running"
    if s in WAITING_APPROVAL_STATUSES:
        return "waiting_approval"
    if s in FAILED_STATUSES:
        return "failed"
    if s in DONE_STATUSES:
        return "done"
    return "other"


def derive_zone(status: str | None) -> Zone:
    """Map a raw status to its attention zone. Derived from the raw status (not
    the column) so `approval`/`awaiting_approval` (a decision is needed →
    ``needs_you``) and `review` (under review → ``in_review``) split even though
    they share the ``waiting_approval`` column."""
    s = _normalise(status)
    if s in FAILED_STATUSES:
        return "needs_you"
    if s in {"approval", "awaiting_approval"}:
        return "needs_you"
    if s == "review":
        return "in_review"
    if s in RUNNING_STATUSES:
        return "working"
    if s in QUEUED_STATUSES:
        return "queued"
    if s in DONE_STATUSES:
        return "ready"
    return "other"


def needs_attention(status: str | None) -> bool:
    """True when a human should look now (a failure or a pending decision)."""
    return derive_zone(status) == "needs_you"


@dataclass(frozen=True)
class DerivedStatus:
    """The full derived view of one status fact."""

    raw: str
    column: Column
    zone: Zone
    needs_attention: bool


def derive_status(status: str | None) -> DerivedStatus:
    return DerivedStatus(
        raw=_normalise(status),
        column=derive_column(status),
        zone=derive_zone(status),
        needs_attention=needs_attention(status),
    )
