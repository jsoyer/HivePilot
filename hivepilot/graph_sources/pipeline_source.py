"""Built-in `pipeline` graph source (Mirador Graph View PRD, Sprint 2).

Renders a single pipeline's stage topology (`pipelines.yaml`) as a DAG —
one `GraphNode` per `PipelineStage`, coloured by that PIPELINE's own LAST
run's per-stage outcome (`state.db` `runs`/`steps` tables via
`hivepilot/services/state_service.py`). Edges are the pipeline's sequential
stage flow, plus a `kind="context"` edge wherever `context_routing_mode`
(`hivepilot/config.py`) is `"keyed"` AND a downstream stage's role declares
an input/optional_input key the upstream stage's role produces (mirrors
`hivepilot.orchestrator._route_prior_context`'s own keyed-routing decision,
without re-executing any run).

TENANT: unlike `plugins_source.py` (pure configuration, no tenant concept),
`runs`/`steps` ARE tenant data. Every `state_service` read in this module
threads `ctx.tenant` through explicitly (`list_all_runs(tenant=...)`) — a
tenant-A token must NEVER see tenant-B's run status. This is the convention
later tenant-scoped graph sources must follow.

Node id namespacing: `stage:<pipeline_name>:<stage_name>` — NOT just
`stage:<stage_name>`. `node_detail(ctx, node_id)` is called by the API layer
with an EMPTY `params` dict (see `get_graph_node_detail_endpoint` in
`hivepilot/services/api_service.py`), so the id itself must carry enough
information to resolve detail without a `?pipeline=` query param.
"""

from __future__ import annotations

from typing import Any

from hivepilot.config import settings
from hivepilot.graph import (
    GraphContext,
    GraphData,
    GraphDetail,
    GraphEdge,
    GraphNode,
    GraphSourceSpec,
)
from hivepilot.plugins import PanelStatSection, PanelTableSection, PanelTextSection
from hivepilot.services.project_service import load_pipelines, load_tasks

_STAGE_PREFIX = "stage:"
_RECENT_RUNS_LIMIT = 5


def _last_pipeline_run(pipeline_name: str, tenant: str) -> dict[str, Any] | None:
    """The most recent whole-pipeline run row for *pipeline_name*, scoped to
    *tenant* only (`state_service.list_all_runs(tenant=...)`). A whole-
    pipeline run is recorded with `project == task == pipeline_name`
    (`Orchestrator._run_pipeline_body`'s `state_service.record_run_start`
    call) — distinct from the many per-project/per-task runs the SAME table
    also holds, which this deliberately does NOT match."""
    from hivepilot.services import state_service

    for run in state_service.list_all_runs(tenant=tenant):
        if run.get("project") == pipeline_name and run.get("task") == pipeline_name:
            return run
    return None


def _recent_pipeline_runs(
    pipeline_name: str, tenant: str, *, limit: int = _RECENT_RUNS_LIMIT
) -> list[dict[str, Any]]:
    from hivepilot.services import state_service

    matches = [
        run
        for run in state_service.list_all_runs(tenant=tenant)
        if run.get("project") == pipeline_name and run.get("task") == pipeline_name
    ]
    return matches[:limit]


def _stage_status(task_cfg: Any, steps_by_name: dict[str, list[dict[str, Any]]]) -> str | None:
    """Best-effort per-stage outcome ("ok"/"warn"/"error") for a single
    `PipelineStage`, derived by matching the `steps` table's `step` column
    against the stage's own `TaskConfig.steps[*].name` values (there is no
    direct stage->step FK in `state.db` — a stage's task can have multiple
    named steps, each individually recorded via
    `state_service.record_step`). "warn" means "no matching step rows found
    for this run" (not yet executed / task has no steps), never a crash."""
    if task_cfg is None:
        return "warn"
    step_names = {step.name for step in task_cfg.steps}
    if not step_names:
        return "warn"
    matched: list[dict[str, Any]] = []
    for name in step_names:
        matched.extend(steps_by_name.get(name, []))
    if not matched:
        return "warn"
    if any(step.get("status") != "success" for step in matched):
        return "error"
    return "ok"


def _format_duration(run: dict[str, Any]) -> str:
    started = run.get("started_at")
    finished = run.get("finished_at")
    if not started or not finished:
        return "n/a"
    try:
        from datetime import datetime

        fmt_candidates = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
        start_dt = finish_dt = None
        for fmt in fmt_candidates:
            try:
                start_dt = datetime.strptime(str(started)[:19], fmt)
                finish_dt = datetime.strptime(str(finished)[:19], fmt)
                break
            except ValueError:
                continue
        if start_dt is None or finish_dt is None:
            return "n/a"
        seconds = max(0, int((finish_dt - start_dt).total_seconds()))
        return f"{seconds}s"
    except Exception:  # noqa: BLE001 - detail rendering must never crash
        return "n/a"


def _stage_role_name(task_cfg: Any) -> str | None:
    if task_cfg is None:
        return None
    return task_cfg.role


def _build_graph(ctx: GraphContext) -> GraphData:
    """`?pipeline=<name>` is REQUIRED (declared in `GraphSourceSpec.params`).
    Missing or unknown pipeline raises -- caught by `run_graph_fetch`'s
    never-raise wrapper and normalized into a single `kind="error"` node
    (200, never a 500), exactly like a malformed/raising source in Sprint 1.
    """
    from hivepilot.roles import ROLES

    pipeline_name = ctx.params.get("pipeline")
    if not pipeline_name:
        raise ValueError("missing required 'pipeline' query parameter")

    pipelines_file = load_pipelines()
    pipeline = pipelines_file.pipelines.get(pipeline_name)
    if pipeline is None:
        raise ValueError(f"unknown pipeline: {pipeline_name!r}")

    tasks_file = load_tasks()

    last_run = _last_pipeline_run(pipeline_name, ctx.tenant)
    steps_by_name: dict[str, list[dict[str, Any]]] = {}
    if last_run is not None:
        from hivepilot.services import state_service

        run_id = last_run.get("id")
        if run_id is not None:
            for step in state_service.get_steps_for_run(int(run_id)):
                steps_by_name.setdefault(step.get("step") or "", []).append(step)

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    prev_id: str | None = None
    prev_role_name: str | None = None
    routing_mode = settings.context_routing_mode

    for stage in pipeline.stages:
        stage_id = f"{_STAGE_PREFIX}{pipeline_name}:{stage.name}"
        task_cfg = tasks_file.tasks.get(stage.task)
        role_name = _stage_role_name(task_cfg)
        status = _stage_status(task_cfg, steps_by_name) if last_run is not None else None
        nodes.append(
            GraphNode(
                id=stage_id,
                label=stage.name,
                kind="stage",
                status=status,
                group=pipeline_name,
                meta={"task": stage.task, "role": role_name or ""},
            )
        )
        if prev_id is not None:
            edges.append(GraphEdge(source=prev_id, target=stage_id, kind="flow"))
            if routing_mode == "keyed" and prev_role_name and role_name:
                prev_role = ROLES.get(prev_role_name)
                role = ROLES.get(role_name)
                if prev_role is not None and role is not None:
                    downstream_inputs = set(role.inputs) | set(role.optional_inputs)
                    if set(prev_role.outputs) & downstream_inputs:
                        edges.append(GraphEdge(source=prev_id, target=stage_id, kind="context"))
        prev_id = stage_id
        prev_role_name = role_name

    return GraphData(source="pipeline", nodes=tuple(nodes), edges=tuple(edges), layout_hint="dag")


def _parse_stage_node_id(node_id: str) -> tuple[str, str] | None:
    if not node_id.startswith(_STAGE_PREFIX):
        return None
    rest = node_id[len(_STAGE_PREFIX) :]
    pipeline_name, sep, stage_name = rest.partition(":")
    if not sep or not pipeline_name or not stage_name:
        return None
    return pipeline_name, stage_name


def _node_detail(ctx: GraphContext, node_id: str) -> GraphDetail | None:
    parsed = _parse_stage_node_id(node_id)
    if parsed is None:
        return None
    pipeline_name, stage_name = parsed

    pipelines_file = load_pipelines()
    pipeline = pipelines_file.pipelines.get(pipeline_name)
    if pipeline is None:
        return None
    stage = next((s for s in pipeline.stages if s.name == stage_name), None)
    if stage is None:
        return None

    tasks_file = load_tasks()
    task_cfg = tasks_file.tasks.get(stage.task)
    role_name = _stage_role_name(task_cfg)

    last_run = _last_pipeline_run(pipeline_name, ctx.tenant)
    steps_by_name: dict[str, list[dict[str, Any]]] = {}
    if last_run is not None:
        from hivepilot.services import state_service

        run_id = last_run.get("id")
        if run_id is not None:
            for step in state_service.get_steps_for_run(int(run_id)):
                steps_by_name.setdefault(step.get("step") or "", []).append(step)

    status = _stage_status(task_cfg, steps_by_name) if last_run is not None else "warn"
    duration = _format_duration(last_run) if last_run is not None else "n/a"

    runner = None
    model = None
    if task_cfg is not None and task_cfg.steps:
        runner = task_cfg.steps[0].runner
        model = task_cfg.options.get("model") if isinstance(task_cfg.options, dict) else None

    sections: list[Any] = [
        PanelStatSection(
            kind="stat",
            label="last outcome",
            value=status or "unknown",
            status=status if status in ("ok", "warn", "error") else None,
        ),
        PanelStatSection(kind="stat", label="run duration", value=duration, status=None),
        PanelTextSection(
            kind="text",
            content=f"role={role_name or '-'} runner={runner or '-'} model={model or '-'}",
        ),
    ]
    recent = _recent_pipeline_runs(pipeline_name, ctx.tenant)
    if recent:
        sections.append(
            PanelTableSection(
                kind="table",
                columns=["started_at", "status"],
                rows=[
                    [str(run.get("started_at") or "-"), str(run.get("status") or "-")]
                    for run in recent
                ],
            )
        )

    return GraphDetail(
        title=stage_name, tags=("stage", status or "unknown"), sections=tuple(sections)
    )


PIPELINE_GRAPH_SOURCE = GraphSourceSpec(
    name="pipeline",
    data=_build_graph,
    node_detail=_node_detail,
    title="Pipeline",
    min_role="read",
    params=("pipeline",),
)
