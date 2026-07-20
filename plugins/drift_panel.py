"""`drift_panel` -- plugin-contributed Mirador `panel` capability
contribution (Mirador Panels sprint).

A counts-only summary of the SAME IaC drift-scan history the `state.db`
`drift_scans` table already holds (Phase 20 D1/D2 --
`hivepilot.services.drift_service.scan_and_record` /
`state_service.record_drift_scan`) that `plugins/drift_graph_source.py`
already renders as a graph -- this plugin renders the same underlying data
as a `panel` (a `stat` section with total/drifted counts + a `table` section
listing recent scans). Mirrors `plugins/drift_graph_source.py`'s structure
and read-only/anti-leak discipline precisely -- see that file's own module
docstring for the full plugin-contract/CPython-3.14-dataclass-bug
rationale, not repeated here.

TENANT: unlike `GraphSourceSpec.data`/`node_detail` (which receive a
`GraphContext` carrying the caller's `tenant`), `PanelSpec["fetch"]` takes
NO arguments -- panels get no caller tenant/`GraphContext`. This plugin
therefore hardcodes single-tenant scope (`default`) only -- never pass
`tenant=None` (which would aggregate cross-tenant data, see
`state_service.get_recent_drift_scans`'s own docstring). This is a
documented known limitation of the `panel` plugin type, not a bug.

Anti-leak: `drift_scans.detail` is redacted at persist time
(`state_service.record_drift_scan`) but must STILL never be surfaced here --
this module only ever reads `project`/`runner`/`status`/`checked_at`/
`to_add`/`to_change`/`to_destroy` off a row, never `detail`.

Opt-in / read-only: gated on `settings.drift_panel_enabled` (default False --
`register()` early-returns `{}` when unset, required by
`tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag`). This
module only ever calls `state_service.get_recent_drift_scans` (a read); it
never calls `record_drift_scan` or any other writer, and has no other side
effect. `_fetch` itself is never wrapped in a try/except -- `run_panel_fetch`
(`hivepilot/plugins.py`) is the sole never-raise choke point, exactly like
`plugins/sample.py`/`plugins/drift_graph_source.py`.
"""

from __future__ import annotations

from typing import Any

_TENANT = "default"

# Generous but bounded -- enough to cover a busy tenant's recent history for
# the panel table without an unbounded query.
_SCAN_FETCH_LIMIT = 50


def _count(value: Any) -> int:
    return int(value) if value is not None else 0


def _fetch() -> dict[str, Any]:
    from hivepilot.services import state_service

    scans = state_service.get_recent_drift_scans(limit=_SCAN_FETCH_LIMIT, tenant=_TENANT)

    if not scans:
        return {"sections": [{"kind": "text", "content": "No drift scans recorded."}]}

    drifted_count = sum(1 for scan in scans if scan.get("status") == "drift")
    stat = {
        "kind": "stat",
        "label": "drift scans",
        "value": f"{len(scans)} scans, {drifted_count} drifted",
        "status": "warn" if drifted_count else "ok",
    }
    table = {
        "kind": "table",
        "columns": [
            "project",
            "checked_at",
            "status",
            "to_add",
            "to_change",
            "to_destroy",
            "runner",
        ],
        "rows": [
            [
                str(scan.get("project") or ""),
                str(scan.get("checked_at") or ""),
                str(scan.get("status") or ""),
                str(_count(scan.get("to_add"))),
                str(_count(scan.get("to_change"))),
                str(_count(scan.get("to_destroy"))),
                str(scan.get("runner") or ""),
            ]
            for scan in scans
        ],
    }
    return {"sections": [stat, table]}


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.drift_panel_enabled:
        return {}

    return {
        "panels": [
            {
                "name": "drift_history",
                "title": "Drift History",
                "min_role": "read",
                "fetch": _fetch,
            }
        ]
    }
