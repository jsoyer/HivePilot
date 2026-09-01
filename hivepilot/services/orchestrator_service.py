"""Orchestrator conversation service (HP-49, Cycle 1 · P2).

The persistent, per-project coordination surface: one durable "Orchestrateur"
Espace per project where the orchestrator decomposes a feature into a
`MissionPlan` (HP-49), posts the breakdown, and (in later slices) spawns the
workers via the delegation primitives (HP-48) and synthesizes their results.

This slice delivers the persistent space + the decomposition entry point; the
plan's `strategy` (HP-69) and per-role model+repli (HP-70) hang off the
returned `MissionPlan`.
"""

from __future__ import annotations

from hivepilot.services import delegation, events, mission_plan, state_service


def _orchestrator_space_title(project: str) -> str:
    return f"Orchestrateur — {project}"


def get_or_create_project_space(project: str, tenant: str = "default") -> int:
    """Return the id of the project's single persistent Orchestrateur Espace,
    creating it (a `room` with a human participant) on first use."""
    title = _orchestrator_space_title(project)
    for space in state_service.list_spaces(tenant):
        if space.get("title") == title:
            return int(space["id"])
    return state_service.create_space(
        [{"type": "human", "id": None}], kind="room", title=title, tenant=tenant
    )


def _plan_summary(plan: mission_plan.MissionPlan) -> str:
    goal = plan.goal.strip().splitlines()[0][:80] if plan.goal.strip() else "mission"
    return f"Découpe « {goal} » — {len(plan.tasks)} task(s), stratégie « {plan.strategy} »."


def _decompose_and_post(
    goal: str, project: str, tenant: str, strategy: str | None = None
) -> tuple[mission_plan.MissionPlan, int]:
    """Decompose `goal` and post the breakdown (with a per-task action trace)
    into the project's Orchestrateur Espace. Returns `(plan, space_id)`. An
    explicit `strategy` (from the UI mode card) overrides the planner's choice;
    an unknown name is ignored (the plan keeps its own valid strategy)."""
    plan = mission_plan.decompose(goal, project)
    if strategy and strategy in mission_plan.STRATEGIES:
        plan.strategy = strategy
    space_id = get_or_create_project_space(project, tenant)
    msg_id = state_service.add_space_message(
        space_id,
        "system",
        _plan_summary(plan),
        tenant=tenant,
        actions=[
            {"label": f"{task.id} · {task.title}", "detail": f"{task.role}: {task.description}"}
            for task in plan.tasks
        ],
    )
    events.emit(
        "space.message",
        "space",
        space_id,
        tenant=tenant,
        payload={"space_id": space_id, "message_id": msg_id, "sender_type": "system"},
    )
    return plan, space_id


def decompose_feature(
    goal: str, project: str, tenant: str = "default", strategy: str | None = None
) -> dict:
    """Decompose `goal` into a `MissionPlan` and surface it in the Orchestrateur
    Espace — a PREVIEW (no spawn). Returns `{plan, space_id}`."""
    plan, space_id = _decompose_and_post(goal, project, tenant, strategy)
    return {"plan": plan.to_dict(), "space_id": space_id}


#: How each merge policy reads to the coder assigned the task (HP-69).
_MERGE_NOTE = {
    "per_task": "code→review→merge",
    "per_branch": "merge sa branche",
    "final": "merge final groupé",
    "none": "sans merge",
}


def _ordered_tasks(
    tasks: list[mission_plan.MissionTask], preset: mission_plan.StrategyPreset
) -> list[mission_plan.MissionTask]:
    """Dispatch order for a strategy. A `sequential` preset honors
    `depends_on` (Kahn topological sort, stable on the plan's own order);
    `parallel` keeps the plan order (partitions fan them out at once). Tolerant
    of unknown or cyclic dependencies — any leftover tasks are appended in
    their original order rather than dropped."""
    if preset.dispatch != "sequential":
        return list(tasks)
    by_id = {t.id: t for t in tasks}
    remaining = {t.id: {d for d in t.depends_on if d in by_id} for t in tasks}
    ordered: list[mission_plan.MissionTask] = []
    while remaining:
        ready = [tid for tid in remaining if not remaining[tid]]
        if not ready:  # cycle / unresolved — append the rest in plan order
            ordered.extend(by_id[t.id] for t in tasks if t.id in remaining)
            break
        for tid in [t.id for t in tasks if t.id in ready]:  # stable
            ordered.append(by_id[tid])
            del remaining[tid]
            for deps in remaining.values():
                deps.discard(tid)
    return ordered


def spawn_plan(
    plan: mission_plan.MissionPlan, project: str, space_id: int, tenant: str = "default"
) -> dict[str, int]:
    """Spawn one background run per task in the plan (HP-48 `spawn_peer`),
    tracing each into the Orchestrateur Espace. Returns `{task_id: run_id}`.

    The plan's STRATEGY (HP-69) preconfigures HOW tasks dispatch and merge: a
    `sequential` strategy spawns in dependency order; a `parallel` one fans
    them out. Each task's spawn trace carries the resolved merge policy so the
    assigned coder knows its merge responsibility (own branch vs a final
    Merger vs a per-task pipeline)."""
    preset = mission_plan.resolve_strategy(plan.strategy)
    merge_note = _MERGE_NOTE.get(preset.merge, preset.merge)
    runs: dict[str, int] = {}
    for task in _ordered_tasks(plan.tasks, preset):
        run_id = delegation.spawn_peer(project, task.title or task.id, task.role, tenant=tenant)
        runs[task.id] = run_id
        msg_id = state_service.add_space_message(
            space_id,
            "system",
            f"→ {task.id} · {task.title} ({task.role}) · run #{run_id} · {merge_note}",
            tenant=tenant,
        )
        events.emit(
            "space.message",
            "space",
            space_id,
            tenant=tenant,
            payload={
                "space_id": space_id,
                "message_id": msg_id,
                "sender_type": "system",
                "run_id": run_id,
            },
        )
    return runs


def launch_mission(
    goal: str, project: str, tenant: str = "default", strategy: str | None = None
) -> dict:
    """Decompose `goal`, post the plan, SPAWN each task as a background run, and
    persist the mission (for tracking + synthesis). Returns
    `{plan, space_id, runs, mission_id}`."""
    plan, space_id = _decompose_and_post(goal, project, tenant, strategy)
    runs = spawn_plan(plan, project, space_id, tenant)
    mission_id = state_service.create_mission(space_id, project, goal, runs, tenant)
    return {
        "plan": plan.to_dict(),
        "space_id": space_id,
        "runs": runs,
        "mission_id": mission_id,
    }


def mission_status(mission: dict) -> dict:
    """Aggregate the live status of a mission's runs (derived from each run's
    status fact via the shared contract, HP-42). `done` is True when every run
    has settled (succeeded / failed / cancelled)."""
    from hivepilot.services.status_contract import DONE_STATUSES, FAILED_STATUSES

    runs = mission.get("runs") or {}
    succeeded = failed = pending = 0
    tasks: dict[str, dict] = {}
    for task_id, run_id in runs.items():
        row = state_service.get_run(int(run_id)) if run_id is not None else None
        raw = ((row or {}).get("status") or "").strip().lower()
        if raw in DONE_STATUSES:
            succeeded += 1
        elif raw in FAILED_STATUSES or raw == "cancelled":
            failed += 1
        else:
            pending += 1
        tasks[task_id] = {"run_id": run_id, "status": raw or None}
    total = len(runs)
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "done": total > 0 and pending == 0,
        "tasks": tasks,
    }


def _post_synthesis(mission: dict, status: dict, tenant: str) -> None:
    space_id = mission.get("space_id")
    if not space_id:
        return
    text = (
        f"Mission terminée — {status['succeeded']}/{status['total']} réussie(s)"
        f", {status['failed']} en échec."
    )
    msg_id = state_service.add_space_message(
        int(space_id),
        "system",
        text,
        tenant=tenant,
        actions=[
            {"label": f"{task_id} · {info['status'] or 'unknown'}"}
            for task_id, info in status["tasks"].items()
        ],
    )
    events.emit(
        "space.message",
        "space",
        int(space_id),
        tenant=tenant,
        payload={"space_id": int(space_id), "message_id": msg_id, "sender_type": "system"},
    )


def check_mission(mission_id: int, tenant: str = "default") -> dict | None:
    """Return the mission's live status and, the first time every run has
    settled, post a one-shot synthesis into its Espace. Idempotent — the
    synthesis is posted at most once. `None` when the mission doesn't exist."""
    mission = state_service.get_mission(mission_id, tenant=tenant)
    if mission is None:
        return None
    status = mission_status(mission)
    synthesized = bool(mission["synthesized"])
    if status["done"] and not synthesized:
        _post_synthesis(mission, status, tenant)
        state_service.mark_mission_synthesized(mission_id, tenant=tenant)
        synthesized = True
    return {"mission_id": mission_id, "status": status, "synthesized": synthesized}
