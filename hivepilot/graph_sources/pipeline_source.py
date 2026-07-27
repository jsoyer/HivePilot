"""Built-in `pipeline` graph source (Mirador Graph View PRD, Sprint 2;
rebuilt for the Pollen "Service Map" cascade view).

Renders a single pipeline's stage topology (`pipelines.yaml`) as a DAG —
one `GraphNode` per `PipelineStage`, coloured by a SELECTED run's per-stage
outcome (`state.db` `runs`/`steps` tables via
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

Status vocabulary (Pollen graph-cascade rebuild): a stage's `GraphNode.status`
is one of `ok` / `error` / `skipped` / `running` / `pending` / `warn` (or
`None` when no run exists yet at all). Stages execute strictly sequentially
(`Orchestrator._run_pipeline_body`'s stage loop), so "evidence" — a matched
real `steps` row OR the `skip:<stage.name>` marker row the orchestrator's two
scope-skip gates now persist (see `hivepilot/orchestrator.py`) — forms a
PREFIX of the stage list for any given run. The first stage past that prefix
is `running` while the run is still in-flight (`runs.status == "running"`,
the literal value `Orchestrator._run_pipeline_body` writes at start and never
changes until a terminal `complete_run` call); every stage further ahead is
`pending` (not reached yet, genuinely no data — never fabricated). Once a run
is no longer in-flight, a stage with no evidence at all is `warn` — an
anomaly (e.g. a crash mid-stage that never persisted), never silently
reported as a real outcome. This is intentionally NOT based on
`runs.finished_at` (may be NULL even for an old, already-terminal test/synth
row) — `runs.status` is the one field `Orchestrator` reliably flips to
`"running"` at start and to a terminal value at the end.

Run selection: `?run_id=` (NOT declared in `GraphSourceSpec.params` — see
that dataclass's own docstring: a source may read ANY raw query-string key,
declaring only the ones that should render as an always-required form
field) lets a caller pick a specific historical run instead of always the
last one. Falls back to the last run — byte-identical to this source's
pre-run-selector behavior — when the param is absent, non-numeric, unknown,
or (fail-closed) belongs to a DIFFERENT tenant than the caller's own.

`GraphData.meta` exposes a run selector's raw materials (`runs`: recent run
summaries for this pipeline, `selected_run_id`, `live`: whether the selected
run is still in-flight) through the generic, arbitrary `GraphData.meta` hook
(`hivepilot/graph.py`) — never a hardcoded field on that closed contract.

Per-stage node metrics (`model`/`tokens`/`cost`/`duration` in `GraphNode.meta`)
are the Pollen translation of the reference mockup's `REQ/S ERR% AVG PODS`
card table — every value is read from a real `steps` row for the selected
run; an em-dash (`—`) renders wherever data is genuinely absent, NEVER a
fabricated zero or plausible-looking placeholder. `duration` is DERIVED (not
directly stored — `steps` has only a single `timestamp` column, no
start/end pair) as the elapsed time between the previous stage's last
evidence timestamp (or the run's own `started_at` for the first stage) and
this stage's own last evidence timestamp — a real, timestamp-backed value,
clearly documented as an approximation rather than a raw stored field.
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
_SKIP_PREFIX = "skip:"
_RECENT_RUNS_LIMIT = 5
_RUN_SELECTOR_LIMIT = 20
_EM_DASH = "—"
_RUNNING_STATUS = "running"


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


def _select_pipeline_run(
    pipeline_name: str, tenant: str, run_id_param: str | None
) -> dict[str, Any] | None:
    """Either the explicitly-requested run (`?run_id=`), or — when absent,
    non-numeric, unknown, or belonging to a DIFFERENT tenant (fail-closed: a
    caller must never select another tenant's run by guessing its numeric
    id) — the last run for this pipeline, exactly as before `run_id`
    selection existed."""
    if run_id_param:
        try:
            run_id_int = int(run_id_param)
        except (TypeError, ValueError):
            run_id_int = None
        if run_id_int is not None:
            from hivepilot.services import state_service

            run = state_service.get_run(run_id_int)
            if (
                run is not None
                and run.get("tenant", "default") == tenant
                and run.get("project") == pipeline_name
                and run.get("task") == pipeline_name
            ):
                return run
    return _last_pipeline_run(pipeline_name, tenant)


def _run_active(run: dict[str, Any] | None) -> bool:
    """Whether *run* is still in-flight — see module docstring for why this
    is `runs.status == "running"` rather than `finished_at is None`."""
    return run is not None and run.get("status") == _RUNNING_STATUS


def _steps_by_key(run_id: int) -> dict[str, list[dict[str, Any]]]:
    from hivepilot.services import state_service

    steps_by_key: dict[str, list[dict[str, Any]]] = {}
    for step in state_service.get_steps_for_run(run_id):
        steps_by_key.setdefault(step.get("step") or "", []).append(step)
    return steps_by_key


def _skip_rows(stage: Any, steps_by_key: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return steps_by_key.get(f"{_SKIP_PREFIX}{stage.name}", [])


def _matched_rows(
    task_cfg: Any, steps_by_key: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    if task_cfg is None:
        return []
    matched: list[dict[str, Any]] = []
    for name in {step.name for step in task_cfg.steps}:
        matched.extend(steps_by_key.get(name, []))
    return matched


def _stage_evidence(rows: list[dict[str, Any]]) -> str | None:
    """`ok` / `error` for a set of real (non-skip) matched step rows, or
    `None` when there are none yet — `None` is distinct from every real
    outcome; the caller (`_resolve_stage_statuses`) turns it into
    `running`/`pending`/`warn` depending on the run's own state."""
    if not rows:
        return None
    if any(row.get("status") != "success" for row in rows):
        return "error"
    return "ok"


def _resolve_stage_statuses(
    stages: list[Any],
    tasks_file: Any,
    steps_by_key: dict[str, list[dict[str, Any]]],
    *,
    run_active: bool,
) -> dict[str, str]:
    """Real per-stage status for EVERY stage of a pipeline, for a run that
    genuinely exists — see module docstring for the full vocabulary."""
    statuses: dict[str, str] = {}
    reached_gap = False
    for stage in stages:
        if reached_gap:
            statuses[stage.name] = "pending" if run_active else "warn"
            continue
        if _skip_rows(stage, steps_by_key):
            statuses[stage.name] = "skipped"
            continue
        task_cfg = tasks_file.tasks.get(stage.task)
        evidence = _stage_evidence(_matched_rows(task_cfg, steps_by_key))
        if evidence is not None:
            statuses[stage.name] = evidence
            continue
        reached_gap = True
        statuses[stage.name] = "running" if run_active else "warn"
    return statuses


def _last_timestamp(rows: list[dict[str, Any]]) -> str | None:
    timestamps = [row.get("timestamp") for row in rows if row.get("timestamp")]
    return max(timestamps) if timestamps else None


def _elapsed_display(start: str | None, end: str | None, *, fallback: str) -> str:
    if not start or not end:
        return fallback
    try:
        from datetime import datetime

        fmt_candidates = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
        start_dt = finish_dt = None
        for fmt in fmt_candidates:
            try:
                start_dt = datetime.strptime(str(start)[:19], fmt)
                finish_dt = datetime.strptime(str(end)[:19], fmt)
                break
            except ValueError:
                continue
        if start_dt is None or finish_dt is None:
            return fallback
        seconds = max(0, int((finish_dt - start_dt).total_seconds()))
        return f"{seconds}s"
    except Exception:  # noqa: BLE001 - detail rendering must never crash
        return fallback


def _format_duration(run: dict[str, Any]) -> str:
    """Whole-RUN duration (`runs.started_at` -> `runs.finished_at`) for the
    existing `_node_detail` "run duration" stat — unchanged fallback
    wording ("n/a") from before this sprint; kept distinct from the NEW
    per-stage derived `duration` metric below, which uses the `—`
    convention this feature explicitly requires for absent data."""
    return _elapsed_display(run.get("started_at"), run.get("finished_at"), fallback="n/a")


def _stage_role_name(task_cfg: Any) -> str | None:
    if task_cfg is None:
        return None
    return task_cfg.role


def _stage_metrics(rows: list[dict[str, Any]], duration: str) -> dict[str, str]:
    """Real, `steps`-row-derived metrics for a single stage's node card —
    the Pollen translation of the reference's `REQ/S ERR% AVG PODS` table.
    Never a fabricated placeholder: every field is `—` when genuinely
    absent, not a misleading `0`."""
    model = next((row.get("model") for row in reversed(rows) if row.get("model")), None)
    tokens_in = [row.get("input_tokens") for row in rows if row.get("input_tokens") is not None]
    tokens_out = [row.get("output_tokens") for row in rows if row.get("output_tokens") is not None]
    costs = [row.get("cost_usd") for row in rows if row.get("cost_usd") is not None]
    tokens_display = _EM_DASH
    if tokens_in or tokens_out:
        tokens_display = f"{sum(tokens_in)}/{sum(tokens_out)}"
    cost_display = f"${sum(costs):.4f}" if costs else _EM_DASH
    return {
        "model": str(model) if model else _EM_DASH,
        "tokens": tokens_display,
        "cost": cost_display,
        "duration": duration,
    }


def _build_graph(ctx: GraphContext) -> GraphData:
    """`?pipeline=<name>` is REQUIRED (declared in `GraphSourceSpec.params`).
    Missing or unknown pipeline raises -- caught by `run_graph_fetch`'s
    never-raise wrapper and normalized into a single `kind="error"` node
    (200, never a 500), exactly like a malformed/raising source in Sprint 1.
    `?run_id=` is OPTIONAL (see module docstring) -- never required, never
    rendered as a mandatory form field by the web `GraphView`.
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

    selected_run = _select_pipeline_run(pipeline_name, ctx.tenant, ctx.params.get("run_id"))
    steps_by_key: dict[str, list[dict[str, Any]]] = {}
    run_active = False
    if selected_run is not None:
        run_id = selected_run.get("id")
        if run_id is not None:
            steps_by_key = _steps_by_key(int(run_id))
        run_active = _run_active(selected_run)

    stage_statuses = (
        _resolve_stage_statuses(pipeline.stages, tasks_file, steps_by_key, run_active=run_active)
        if selected_run is not None
        else {}
    )

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    prev_id: str | None = None
    prev_role_name: str | None = None
    routing_mode = settings.context_routing_mode
    duration_cursor = selected_run.get("started_at") if selected_run is not None else None

    for stage in pipeline.stages:
        stage_id = f"{_STAGE_PREFIX}{pipeline_name}:{stage.name}"
        task_cfg = tasks_file.tasks.get(stage.task)
        role_name = _stage_role_name(task_cfg)
        status = stage_statuses.get(stage.name) if selected_run is not None else None

        evidence_rows = _skip_rows(stage, steps_by_key) or _matched_rows(task_cfg, steps_by_key)
        stage_last_ts = _last_timestamp(evidence_rows)
        duration = _elapsed_display(duration_cursor, stage_last_ts, fallback=_EM_DASH)
        if stage_last_ts:
            duration_cursor = stage_last_ts
        metrics = _stage_metrics(evidence_rows, duration)

        nodes.append(
            GraphNode(
                id=stage_id,
                label=stage.name,
                kind="stage",
                status=status,
                group=pipeline_name,
                badges=(role_name,) if role_name else (),
                meta={"task": stage.task, "role": role_name or "", **metrics},
            )
        )
        if prev_id is not None:
            edge_label = None
            if metrics["duration"] != _EM_DASH or metrics["tokens"] != _EM_DASH:
                edge_label = f"{metrics['duration']} · {metrics['tokens']}"
            edges.append(GraphEdge(source=prev_id, target=stage_id, kind="flow", label=edge_label))
            if routing_mode == "keyed" and prev_role_name and role_name:
                prev_role = ROLES.get(prev_role_name)
                role = ROLES.get(role_name)
                if prev_role is not None and role is not None:
                    downstream_inputs = set(role.inputs) | set(role.optional_inputs)
                    if set(prev_role.outputs) & downstream_inputs:
                        edges.append(GraphEdge(source=prev_id, target=stage_id, kind="context"))
        prev_id = stage_id
        prev_role_name = role_name

    recent_runs = _recent_pipeline_runs(pipeline_name, ctx.tenant, limit=_RUN_SELECTOR_LIMIT)
    meta = {
        "runs": [
            {
                "id": run.get("id"),
                "started_at": run.get("started_at"),
                "status": run.get("status"),
            }
            for run in recent_runs
        ],
        "selected_run_id": selected_run.get("id") if selected_run is not None else None,
        "live": run_active,
    }

    return GraphData(
        source="pipeline", nodes=tuple(nodes), edges=tuple(edges), layout_hint="dag", meta=meta
    )


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

    # `node_detail` is always called with an EMPTY `params` dict (see module
    # docstring) — it has no way to honor a caller's `?run_id=` selection,
    # so it keeps its pre-existing behavior of always describing the LAST
    # run, exactly as before `run_id` selection existed on `_build_graph`.
    last_run = _last_pipeline_run(pipeline_name, ctx.tenant)
    steps_by_key: dict[str, list[dict[str, Any]]] = {}
    run_active = False
    if last_run is not None:
        run_id = last_run.get("id")
        if run_id is not None:
            steps_by_key = _steps_by_key(int(run_id))
        run_active = _run_active(last_run)

    if last_run is not None:
        stage_statuses = _resolve_stage_statuses(
            pipeline.stages, tasks_file, steps_by_key, run_active=run_active
        )
        status = stage_statuses.get(stage_name, "warn")
    else:
        status = "warn"

    duration = _format_duration(last_run) if last_run is not None else "n/a"

    runner = None
    model = None
    if task_cfg is not None and task_cfg.steps:
        runner = task_cfg.steps[0].runner
        model = task_cfg.options.get("model") if isinstance(task_cfg.options, dict) else None

    evidence_rows = _skip_rows(stage, steps_by_key) or _matched_rows(task_cfg, steps_by_key)
    stage_metrics = _stage_metrics(evidence_rows, _EM_DASH)

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
        PanelTableSection(
            kind="table",
            columns=["metric", "value"],
            rows=[
                ["model", stage_metrics["model"]],
                ["tokens (in/out)", stage_metrics["tokens"]],
                ["cost", stage_metrics["cost"]],
            ],
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
