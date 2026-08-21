"""`example_graph_source` -- example `graph_sources` plugin capability
contribution (Mirador Graph View PRD, Sprint 4).

Demonstrates the minimal shape any plugin can contribute via
`register()["graph_sources"] = [GraphSourceSpec, ...]` (see
`hivepilot/plugins.py`'s module-level comment above `_scan_local_plugins`
for the full contract, and `hivepilot/graph.py` for the `GraphSourceSpec`
dataclass itself -- reused here, not redefined).

`run-lineage`: given a `?run=<id>` query param, renders a single run's
lineage as a small DAG -- one `run` node, its recorded `step`s, and any
`verdict`s (debate-judge / challenge-arbiter) adjudicated against it, all
read via `hivepilot.services.state_service`. Mirrors
`hivepilot.graph_sources.pipeline_source`'s tenant discipline (NOT
`plugins_source.py`'s tenant-free pattern, since run/step/verdict rows ARE
tenant data): every lookup is threaded through `ctx.tenant`, and a run id is
only ever resolved via `state_service.list_all_runs(tenant=ctx.tenant)` --
never `state_service.get_run(run_id)` directly, which is NOT tenant-filtered
and would let a caller probe another tenant's run lineage by numeric id
alone.

Opt-in / non-destructive: this plugin is READ-ONLY -- every
`state_service` call it makes is a read (`list_all_runs`,
`get_steps_for_run`, `list_recent_verdicts`); it never calls a writer
(`record_run_start`/`record_step`/`complete_run`/`record_verdict`/
`record_interaction`) and has no other side effect. Gated on
`settings.example_graph_source_enabled` -- default OFF, opt-in, same
structural pattern `plugins/sample.py` / `plugins/sample_skill.py` use
(`register()` early-returns `{}` when the flag is False). The flag itself
is declared on `hivepilot.config.Settings` (required by
`tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag` --
every `plugins/*.py` stem must have a matching `Settings.<stem>_enabled`
field). Independently of that flag, the plugin can also be disabled the
same way any local-file plugin can: `HIVEPILOT_PLUGINS_DISABLED=
example_graph_source` (`settings.plugins_disabled`, honored centrally by
`hivepilot.plugins._scan_local_plugins` BEFORE this module is even exec'd).

Deliberately NOT a `@dataclass`: local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / direct `compile()`+`exec()`
(`hivepilot.plugins._scan_local_plugins`), which never registers this
module in `sys.modules`. This file defines no local dataclass of its own
(it only IMPORTS `GraphSourceSpec`, already defined -- and already fully
processed by the `@dataclass` decorator -- inside the properly-imported
`hivepilot.graph` module), so the CPython 3.14 `dataclasses`/unregistered-
module bug documented in `plugins/rtk.py` does not apply here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hivepilot.graph import GraphData, GraphDetail, GraphEdge, GraphNode, GraphSourceSpec
from hivepilot.plugins import PanelStatSection, PanelTextSection

if TYPE_CHECKING:
    from hivepilot.graph import GraphContext

_RUN_PREFIX = "run:"
_STEP_PREFIX = "step:"
_VERDICT_PREFIX = "verdict:"


def _status_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _find_tenant_run(run_id: int, tenant: str) -> dict[str, Any] | None:
    """Resolve *run_id* ONLY via the caller's own tenant-filtered run list --
    see module docstring's tenant discipline note."""
    from hivepilot.services import state_service

    return next(
        (r for r in state_service.list_all_runs(tenant=tenant) if r.get("id") == run_id),
        None,
    )


def _parse_prefixed(node_id: str, prefix: str) -> str | None:
    if not node_id.startswith(prefix):
        return None
    return node_id[len(prefix) :]


def _build_graph(ctx: "GraphContext") -> GraphData:
    """`?run=<id>` is REQUIRED (declared in `GraphSourceSpec.params`).
    Missing/non-numeric/unknown-for-this-tenant `run` raises -- caught by
    `run_graph_fetch`'s never-raise wrapper (exactly like every built-in
    source) and normalized into a single `kind="error"` node, never a 500.
    """
    from hivepilot.services import state_service

    raw_run_id = ctx.params.get("run")
    if not raw_run_id:
        raise ValueError("missing required 'run' query parameter")
    try:
        run_id = int(raw_run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid 'run' query parameter: {raw_run_id!r}") from exc

    run = _find_tenant_run(run_id, ctx.tenant)
    if run is None:
        raise ValueError(f"run {run_id} not found for this tenant")

    run_node_id = f"{_RUN_PREFIX}{run_id}"
    nodes: list[GraphNode] = [
        GraphNode(
            id=run_node_id,
            label=f"run #{run_id}",
            kind="run",
            status=_status_str(run.get("status")),
            group=str(run.get("project") or ""),
            meta={"project": run.get("project") or "", "task": run.get("task") or ""},
        )
    ]
    edges: list[GraphEdge] = []

    for step in state_service.get_steps_for_run(run_id):
        step_node_id = f"{_STEP_PREFIX}{run_id}:{step.get('id')}"
        nodes.append(
            GraphNode(
                id=step_node_id,
                label=str(step.get("step") or "step"),
                kind="step",
                status=_status_str(step.get("status")),
                group="step",
                meta={
                    "provider": step.get("provider") or "",
                    "model": step.get("model") or "",
                },
            )
        )
        edges.append(GraphEdge(source=run_node_id, target=step_node_id, kind="ran"))

    for verdict in state_service.list_recent_verdicts(run_id=run_id):
        verdict_node_id = f"{_VERDICT_PREFIX}{run_id}:{verdict.get('id')}"
        confidence = verdict.get("confidence")
        nodes.append(
            GraphNode(
                id=verdict_node_id,
                label=f"{verdict.get('kind') or 'verdict'}: {verdict.get('decision') or '-'}",
                kind="verdict",
                status=_status_str(verdict.get("decision")),
                group="verdict",
                meta={
                    "role": verdict.get("role") or "",
                    "confidence": "" if confidence is None else str(confidence),
                },
            )
        )
        edges.append(GraphEdge(source=run_node_id, target=verdict_node_id, kind="adjudicated"))

    return GraphData(
        source="run-lineage", nodes=tuple(nodes), edges=tuple(edges), layout_hint="dag"
    )


def _node_detail(ctx: "GraphContext", node_id: str) -> GraphDetail | None:  # noqa: PLR0911
    from hivepilot.services import state_service

    rest = _parse_prefixed(node_id, _RUN_PREFIX)
    if rest is not None:
        try:
            run_id = int(rest)
        except ValueError:
            return None
        run = _find_tenant_run(run_id, ctx.tenant)
        if run is None:
            return None
        return GraphDetail(
            title=f"run #{run_id}",
            tags=("run", str(run.get("status") or "")),
            sections=(
                PanelTextSection(
                    kind="text",
                    content=f"project={run.get('project') or '-'} task={run.get('task') or '-'}",
                ),
            ),
        )

    rest = _parse_prefixed(node_id, _STEP_PREFIX)
    if rest is not None:
        run_part, _sep, step_id_part = rest.partition(":")
        try:
            run_id = int(run_part)
            step_id = int(step_id_part)
        except ValueError:
            return None
        if _find_tenant_run(run_id, ctx.tenant) is None:
            return None
        step = next(
            (s for s in state_service.get_steps_for_run(run_id) if s.get("id") == step_id),
            None,
        )
        if step is None:
            return None
        return GraphDetail(
            title=str(step.get("step") or "step"),
            tags=("step", str(step.get("status") or "")),
            sections=(
                PanelTextSection(
                    kind="text",
                    content=f"provider={step.get('provider') or '-'} model={step.get('model') or '-'}",
                ),
            ),
        )

    rest = _parse_prefixed(node_id, _VERDICT_PREFIX)
    if rest is not None:
        run_part, _sep, verdict_id_part = rest.partition(":")
        try:
            run_id = int(run_part)
            verdict_id = int(verdict_id_part)
        except ValueError:
            return None
        if _find_tenant_run(run_id, ctx.tenant) is None:
            return None
        verdict = next(
            (
                v
                for v in state_service.list_recent_verdicts(run_id=run_id)
                if v.get("id") == verdict_id
            ),
            None,
        )
        if verdict is None:
            return None
        return GraphDetail(
            title=f"{verdict.get('kind') or 'verdict'} #{verdict_id}",
            tags=("verdict", str(verdict.get("decision") or "")),
            sections=(
                PanelStatSection(
                    kind="stat",
                    label="decision",
                    value=str(verdict.get("decision") or "-"),
                    status=None,
                ),
                PanelTextSection(
                    kind="text",
                    content=(
                        f"role={verdict.get('role') or '-'} confidence={verdict.get('confidence')}"
                    ),
                ),
            ),
        )

    return None


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.example_graph_source_enabled:
        return {}

    return {
        "graph_sources": [
            GraphSourceSpec(
                name="run-lineage",
                data=_build_graph,
                node_detail=_node_detail,
                title="Run Lineage (example)",
                min_role="read",
                params=("run",),
            )
        ]
    }
