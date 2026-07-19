"""`drift_graph_source` -- plugin-contributed `drift` `graph_sources`
capability contribution (Mirador GraphSources plugins sprint).

Demonstrates that a product-choice item can be "just another GraphSource,
zero renderer change": renders the SAME IaC drift-scan history the
`state.db` `drift_scans` table already holds (Phase 20 D1/D2 --
`hivepilot.services.drift_service.scan_and_record` / `state_service.
record_drift_scan`) as a small graph -- one `project` node per project with
at least one recorded scan, and a `drift_scan` node per recorded scan (newest
first) showing `drifted`/`ok`/`error` status plus the plan-summary add/
change/destroy COUNTS (integers, taken straight from the persisted row --
`state_service.record_drift_scan` already stripped the raw plan stdout at
persist time, see that function's own anti-leak docstring; this source never
re-reads or re-derives counts from anything but those three integer
columns). `node_detail` shows a single scan's counts + `checked_at`
timestamp. Mirrors `plugins/example_graph_source.py`'s structure precisely
-- see that file's own module docstring for the full plugin-contract/
CPython-3.14-dataclass-bug rationale, not repeated here.

TENANT: `drift_scans` rows ARE tenant data (Phase 20 D2's `tenant` column,
same discipline `hivepilot.graph_sources.pipeline_source` and
`plugins/example_graph_source.py` already follow for `runs`/`steps`). Every
lookup here threads `ctx.tenant` through explicitly --
`state_service.get_recent_drift_scans(tenant=ctx.tenant)` /
`state_service.get_drift_baseline(project, tenant=ctx.tenant)` -- NEVER the
un-scoped `tenant=None` form (which returns every tenant's rows, see that
function's own docstring), so a tenant-A token can never see tenant-B's
drift history. `_node_detail`'s scan lookup re-runs the SAME tenant-scoped
list rather than any tenant-free by-id getter (there isn't one) -- exactly
the `_find_tenant_run` idiom `example_graph_source.py` uses for runs.

Opt-in / read-only: gated on `settings.drift_graph_source_enabled` (default
False -- `register()` early-returns `{}` when unset, required by
`tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag`). This
module only ever calls `state_service.get_recent_drift_scans` /
`get_drift_baseline` (both reads); it never calls `record_drift_scan` or any
other writer, and has no other side effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hivepilot.graph import GraphData, GraphDetail, GraphEdge, GraphNode, GraphSourceSpec
from hivepilot.plugins import PanelStatSection, PanelTextSection

if TYPE_CHECKING:
    from hivepilot.graph import GraphContext

_PROJECT_PREFIX = "project:"
_SCAN_PREFIX = "scan:"

# Generous but bounded -- enough to cover a busy tenant's full recent
# history for both the graph render and node_detail's by-id lookup (there is
# no dedicated by-id getter, see module docstring), without an unbounded
# query.
_SCAN_FETCH_LIMIT = 500


def _status_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _count(value: Any) -> int:
    return int(value) if value is not None else 0


def _fetch_tenant_scans(ctx: "GraphContext") -> list[dict[str, Any]]:
    from hivepilot.services import state_service

    project_filter = ctx.params.get("project") or None
    return state_service.get_recent_drift_scans(
        project_filter, limit=_SCAN_FETCH_LIMIT, tenant=ctx.tenant
    )


def _build_graph(ctx: "GraphContext") -> GraphData:
    from hivepilot.services import state_service

    scans = _fetch_tenant_scans(ctx)

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_projects: set[str] = set()
    baseline_ids: dict[str, int | None] = {}

    for scan in scans:
        project = str(scan.get("project") or "")
        project_node_id = f"{_PROJECT_PREFIX}{project}"
        if project not in seen_projects:
            seen_projects.add(project)
            baseline = state_service.get_drift_baseline(project, tenant=ctx.tenant)
            baseline_ids[project] = baseline.get("id") if baseline else None
            nodes.append(
                GraphNode(
                    id=project_node_id,
                    label=project,
                    kind="project",
                    status=_status_str(scan.get("status")),
                    group="drift",
                    meta={"runner": scan.get("runner") or ""},
                )
            )

        scan_id = scan.get("id")
        scan_node_id = f"{_SCAN_PREFIX}{scan_id}"
        is_baseline = baseline_ids.get(project) == scan_id
        nodes.append(
            GraphNode(
                id=scan_node_id,
                label=str(scan.get("checked_at") or f"scan #{scan_id}"),
                kind="drift_scan",
                status=_status_str(scan.get("status")),
                group=project,
                badges=("baseline",) if is_baseline else (),
                meta={
                    "to_add": _count(scan.get("to_add")),
                    "to_change": _count(scan.get("to_change")),
                    "to_destroy": _count(scan.get("to_destroy")),
                },
            )
        )
        edges.append(GraphEdge(source=project_node_id, target=scan_node_id, kind="scanned"))

    return GraphData(source="drift", nodes=tuple(nodes), edges=tuple(edges), layout_hint="dag")


def _find_tenant_scan(scan_id: int, ctx: "GraphContext") -> dict[str, Any] | None:
    """Resolve *scan_id* ONLY via the caller's own tenant-scoped scan list --
    see module docstring's tenant discipline note."""
    return next((s for s in _fetch_tenant_scans(ctx) if s.get("id") == scan_id), None)


def _node_detail(ctx: "GraphContext", node_id: str) -> GraphDetail | None:
    if not node_id.startswith(_SCAN_PREFIX):
        return None
    rest = node_id[len(_SCAN_PREFIX) :]
    try:
        scan_id = int(rest)
    except ValueError:
        return None

    scan = _find_tenant_scan(scan_id, ctx)
    if scan is None:
        return None

    status = _status_str(scan.get("status"))
    return GraphDetail(
        title=f"{scan.get('project') or '-'} drift scan #{scan_id}",
        tags=("drift_scan", status or "unknown"),
        sections=(
            PanelStatSection(
                kind="stat", label="to add", value=str(_count(scan.get("to_add"))), status=None
            ),
            PanelStatSection(
                kind="stat",
                label="to change",
                value=str(_count(scan.get("to_change"))),
                status=None,
            ),
            PanelStatSection(
                kind="stat",
                label="to destroy",
                value=str(_count(scan.get("to_destroy"))),
                status=None,
            ),
            PanelTextSection(
                kind="text",
                content=(
                    f"checked_at={scan.get('checked_at') or '-'} runner={scan.get('runner') or '-'}"
                ),
            ),
        ),
    )


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.drift_graph_source_enabled:
        return {}

    return {
        "graph_sources": [
            GraphSourceSpec(
                name="drift",
                data=_build_graph,
                node_detail=_node_detail,
                title="Infrastructure Drift",
                min_role="read",
                params=("project",),
            )
        ]
    }
