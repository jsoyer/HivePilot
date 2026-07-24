"""`headroom_panel` -- plugin-contributed Mirador `panel` capability
contribution (Headroom Efficiency Panel sprint).

A stats-only summary of the cumulative headroom compression efficiency the
`state.db` `headroom_compressions` table holds
(`hivepilot.services.headroom_metrics.record_compression`, written by
`plugins/headroom.py`'s `before_step` hook) -- total compressions, cumulative
chars saved, a best-effort estimated tokens-saved figure, and the avg/p95
compression ratio. Mirrors `plugins/drift_panel.py`'s structure and
read-only/anti-leak discipline precisely -- see that file's own module
docstring for the full plugin-contract/CPython-3.14-dataclass-bug rationale,
not repeated here.

TENANT: unlike `GraphSourceSpec.data`/`node_detail` (which receive a
`GraphContext` carrying the caller's `tenant`), `PanelSpec["fetch"]` takes
NO arguments -- panels get no caller tenant/`GraphContext`. This plugin
therefore hardcodes single-tenant scope (`default`) only -- never pass
`tenant=None` (which would aggregate cross-tenant data). This is a
documented known limitation of the `panel` plugin type, not a bug (same
limitation `plugins/drift_panel.py`/`plugins/autopilot_panel.py` already
carry).

Opt-in / read-only: gated on `settings.headroom_panel_enabled` (default
False -- `register()` early-returns `{}` when unset, required by
`tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag`). This
module only ever calls `headroom_metrics.efficiency_summary` (a read); it
never calls `record_compression` or any other writer, and has no other side
effect. `_fetch` itself is never wrapped in a try/except -- `run_panel_fetch`
(`hivepilot/plugins.py`) is the sole never-raise choke point, exactly like
`plugins/sample.py`/`plugins/drift_panel.py`.

Zero-safe: `efficiency_summary` returns all-zero values (never fabricated)
when nothing has been recorded yet -- rendered here as a single `text`
section, mirroring `plugins/drift_panel.py`'s "No drift scans recorded."
empty state, rather than a misleading all-zero stat block.
"""

from __future__ import annotations

from typing import Any

_TENANT = "default"


def _stat(label: str, value: str) -> dict[str, Any]:
    return {"kind": "stat", "label": label, "value": value, "status": None}


def _fetch() -> dict[str, Any]:
    from hivepilot.services import headroom_metrics

    summary = headroom_metrics.efficiency_summary(tenant=_TENANT)

    if summary["total_compressions"] == 0:
        return {"sections": [{"kind": "text", "content": "No headroom compressions recorded yet."}]}

    return {
        "sections": [
            _stat("compressions", str(summary["total_compressions"])),
            _stat("chars saved", str(summary["chars_saved"])),
            _stat("est. tokens saved", str(int(summary["est_tokens_saved"]))),
            _stat("avg ratio", f"{summary['avg_ratio']:.3f}"),
            _stat("p95 ratio", f"{summary['p95_ratio']:.3f}"),
        ]
    }


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.headroom_panel_enabled:
        return {}

    return {
        "panels": [
            {
                "name": "headroom-efficiency",
                "title": "Headroom efficiency",
                "min_role": "read",
                "fetch": _fetch,
            }
        ]
    }
