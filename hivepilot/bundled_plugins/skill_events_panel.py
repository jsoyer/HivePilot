"""`skill_events_panel` -- Pollen panel for HP-104 skill-cycle ranking.

Read-only top/bottom of **measured** skills. A skill with no `selected`
events has no rate and is omitted — absence of measurement is not zero
(OpenSpace ``record_skill_event`` ranking pattern, rewritten; no cloud,
no pickle).

TENANT: ``PanelSpec["fetch"]`` takes no arguments, so this plugin hardcodes
single-tenant scope (``default``) — never ``tenant=None``. Same documented
limitation as ``drift_panel`` / ``headroom_panel``.

Opt-in: gated on ``settings.skill_events_panel_enabled`` (default False).
``_fetch`` is never wrapped in try/except — ``run_panel_fetch`` is the
never-raise choke point.
"""

from __future__ import annotations

from typing import Any

_TENANT = "default"
_RANK_LIMIT = 5


def _rate_cell(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"{rate:.0%}"


def _table(title_rows: list[list[str]], *, columns: list[str]) -> dict[str, Any]:
    return {"kind": "table", "columns": columns, "rows": title_rows}


def _rows(stats: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [
            str(row.get("skill_name") or ""),
            str(row.get("selected") or 0),
            str(row.get("completed") or 0),
            str(row.get("fallback") or 0),
            str(row.get("excluded") or 0),
            _rate_cell(row.get("rate")),
        ]
        for row in stats
    ]


def _fetch() -> dict[str, Any]:
    from hivepilot.skill_events import rank_skills

    ranking = rank_skills(tenant=_TENANT, limit=_RANK_LIMIT)
    if not ranking.top and not ranking.bottom:
        if ranking.unmeasured:
            return {
                "sections": [
                    {
                        "kind": "stat",
                        "label": "unmeasured skills",
                        "value": str(ranking.unmeasured),
                        "status": None,
                    },
                    {
                        "kind": "text",
                        "content": "Events exist but no skill has a selected count, so none are ranked (absence ≠ zero).",
                    },
                ]
            }
        return {
            "sections": [
                {
                    "kind": "text",
                    "content": "No measured skill-cycle events yet. Unmeasured skills are omitted (not zero).",
                }
            ]
        }

    columns = ["skill", "selected", "completed", "fallback", "excluded", "rate"]
    sections: list[dict[str, Any]] = [
        {
            "kind": "stat",
            "label": "unmeasured skills",
            "value": str(ranking.unmeasured),
            "status": None,
        },
        {
            "kind": "text",
            "content": "Top skills (measured success rate: completed / selected)",
        },
        _table(_rows([row.to_dict() for row in ranking.top]), columns=columns),
        {
            "kind": "text",
            "content": "Bottom skills (measured only; missing rate is not 0%)",
        },
        _table(_rows([row.to_dict() for row in ranking.bottom]), columns=columns),
    ]
    return {"sections": sections}


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.skill_events_panel_enabled:
        return {}

    return {
        "panels": [
            {
                "name": "skill-cycle",
                "title": "Skill cycle",
                "min_role": "read",
                "fetch": _fetch,
            }
        ]
    }
