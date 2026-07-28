"""Pure formatting helpers for TUI tables — no textual dependency."""

from __future__ import annotations

from typing import Any

from hivepilot.utils import display_time

INTERACTION_COLUMNS = ("Run", "Actor", "Action", "Target", "Summary", "Timestamp")


def interaction_rows(interactions: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    """Convert interaction dicts to display-ready string tuples for the TUI table.

    fix/linear-sync-display-time: `timestamp` is stored via
    `state_service.record_interaction` (naive-UTC, no marker) -- the same
    bug class `display_time.to_display` exists to fix. Routed through that
    one shared helper so the Pollen dashboard's Interactions tab never
    renders a bare, unmarked time that reads as local while actually being
    UTC.
    """
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
                display_time.to_display(i.get("timestamp")),
            )
        )
    return rows
