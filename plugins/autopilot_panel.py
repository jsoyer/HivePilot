"""`autopilot_panel` -- plugin-contributed Mirador `panel` capability
contribution (Mirador Panels sprint).

A read-only composition over the autopilot queue/policy surface
(`hivepilot.services.autopilot_queue` / `hivepilot.services.autopilot_policy`
-- Autopilot loop PRD): control status (paused/stopped), a per-state queue
breakdown, a budget burn-down, and an "awaiting human" table. Mirrors the
`autopilot status` CLI command's read composition (`hivepilot/cli.py`'s
`autopilot_status`) plus a budget burn-down, rendered as `panel` sections
instead of `typer.echo` lines. Mirrors `plugins/drift_panel.py`'s structure
and read-only discipline precisely -- see that file's own module docstring
for the full plugin-contract/CPython-3.14-dataclass-bug rationale, not
repeated here.

Budget burn-down granularity: `autopilot_queue.spent_today_usd` is
TENANT-WIDE (no per-project parameter) -- it is rendered as its own single
`stat` section ("tenant spent today: $X"), computed ONCE, never inside the
per-project loop, so it can never be misread as a per-project figure.
`autopilot_policy.get_autopilot_policy(project).budget_daily_usd`, by
contrast, genuinely IS per-project, and is the only per-project column in
the budget table (`project`, `budget_usd`).

TENANT: unlike a `GraphSourceSpec`, `PanelSpec["fetch"]` takes NO arguments
-- panels get no caller tenant/`GraphContext`. This plugin therefore
hardcodes single-tenant scope (`default`) only -- never pass `tenant=None`
(which would aggregate cross-tenant data). This is a documented known
limitation of the `panel` plugin type, not a bug.

Untrusted text: a queue row's `reason` is plugin/human-authored free text
(`autopilot_queue.enqueue`'s `reason` argument) -- shape-validated by
`normalize_panel_data` (a table cell must be a string) but never
editorialized/escaped here; that is the renderer's job (see
`hivepilot/plugins.py`'s `PanelData` docstring: "Renderers must treat these
strings as untrusted").

Opt-in / read-only: gated on `settings.autopilot_panel_enabled` (default
False -- `register()` early-returns `{}` when unset, required by
`tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag`). This
module only ever calls `autopilot_queue.list_queue`/`is_paused`/
`is_stopped`/`spent_today_usd` and `autopilot_policy.get_autopilot_policy`
(all reads); it never calls `enqueue`/`mark`/`promote`/`veto`/`pause`/
`resume`/`stop` or any other writer, and has no other side effect. `_fetch`
itself is never wrapped in a try/except -- `run_panel_fetch`
(`hivepilot/plugins.py`) is the sole never-raise choke point, exactly like
`plugins/sample.py`/`plugins/drift_panel.py`.
"""

from __future__ import annotations

from typing import Any

_TENANT = "default"

# States a human still needs to act on -- mirrors the lifecycle documented
# in `autopilot_queue.py`'s module docstring (`proposed -> queued -> running
# -> done | blocked | vetoed`): `proposed`/`queued` await promotion/dispatch,
# `blocked` means a gate condition failed and stayed unresolved.
_AWAITING_HUMAN_STATES = ("proposed", "queued", "blocked")


def _fetch() -> dict[str, Any]:
    from hivepilot.services import autopilot_policy, autopilot_queue

    paused = autopilot_queue.is_paused(tenant=_TENANT)
    stopped = autopilot_queue.is_stopped(tenant=_TENANT)
    items = autopilot_queue.list_queue(tenant=_TENANT)

    if stopped:
        status_value, status = "stopped", "warn"
    elif paused:
        status_value, status = "paused", "warn"
    else:
        status_value, status = "active", "ok"
    status_stat = {
        "kind": "stat",
        "label": "autopilot",
        "value": status_value,
        "status": status,
    }

    if not items:
        return {
            "sections": [
                status_stat,
                {"kind": "text", "content": "Autopilot queue empty."},
            ]
        }

    counts: dict[str, int] = {}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    counts_table = {
        "kind": "table",
        "columns": ["state", "count"],
        "rows": [[state, str(count)] for state, count in sorted(counts.items())],
    }

    # `spent_today_usd` is TENANT-WIDE (no per-project parameter) -- computed
    # ONCE here, outside the per-project loop, and rendered as its own `stat`
    # section (never as a per-project table column) so it can never be
    # misread as a per-project figure. `budget_daily_usd` (below), by
    # contrast, genuinely IS per-project (`get_autopilot_policy(project)`).
    tenant_spent = autopilot_queue.spent_today_usd(tenant=_TENANT)
    tenant_spent_stat = {
        "kind": "stat",
        "label": "tenant spent today",
        "value": f"${tenant_spent:.2f}",
        "status": None,
    }

    projects = sorted({item.project for item in items})
    budget_rows: list[list[str]] = []
    for project in projects:
        policy = autopilot_policy.get_autopilot_policy(project)
        budget = policy.budget_daily_usd
        budget_str = f"{budget:.2f}" if budget is not None else "-"
        budget_rows.append([project, budget_str])
    budget_table = {
        "kind": "table",
        "columns": ["project", "budget_usd"],
        "rows": budget_rows,
    }

    awaiting_rows = [
        [item.project, item.pipeline, item.state, str(item.reason or "")]
        for item in items
        if item.state in _AWAITING_HUMAN_STATES
    ]
    awaiting_table = {
        "kind": "table",
        "columns": ["project", "pipeline", "state", "reason"],
        "rows": awaiting_rows,
    }

    return {
        "sections": [
            status_stat,
            counts_table,
            tenant_spent_stat,
            budget_table,
            awaiting_table,
        ]
    }


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.autopilot_panel_enabled:
        return {}

    return {
        "panels": [
            {
                "name": "autopilot_status",
                "title": "Autopilot",
                "min_role": "read",
                "fetch": _fetch,
            }
        ]
    }
