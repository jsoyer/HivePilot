"""Pure formatting helpers for TUI tables — no textual dependency."""

from __future__ import annotations

from typing import Any

INTERACTION_COLUMNS = ("Run", "Actor", "Action", "Target", "Summary", "Timestamp")


def interaction_rows(interactions: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    """Convert interaction dicts to display-ready string tuples for the TUI table."""
    rows: list[tuple[str, ...]] = []
    for i in interactions:
        run_id = i.get("run_id")
        rows.append(
            (
                str(run_id) if run_id is not None else "-",
                i.get("actor") or "",
                i.get("action") or "",
                i.get("target") or "all",
                (i.get("summary") or "")[:80],
                i.get("timestamp") or "",
            )
        )
    return rows
