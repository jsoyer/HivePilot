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
    goal: str, project: str, tenant: str
) -> tuple[mission_plan.MissionPlan, int]:
    """Decompose `goal` and post the breakdown (with a per-task action trace)
    into the project's Orchestrateur Espace. Returns `(plan, space_id)`."""
    plan = mission_plan.decompose(goal, project)
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


def decompose_feature(goal: str, project: str, tenant: str = "default") -> dict:
    """Decompose `goal` into a `MissionPlan` and surface it in the Orchestrateur
    Espace — a PREVIEW (no spawn). Returns `{plan, space_id}`."""
    plan, space_id = _decompose_and_post(goal, project, tenant)
    return {"plan": plan.to_dict(), "space_id": space_id}


def spawn_plan(
    plan: mission_plan.MissionPlan, project: str, space_id: int, tenant: str = "default"
) -> dict[str, int]:
    """Spawn one background run per task in the plan (HP-48 `spawn_peer`),
    tracing each into the Orchestrateur Espace. Returns `{task_id: run_id}`.

    Dependency ordering (`task.depends_on`) is carried by the plan and honored
    by the execution STRATEGY (HP-69); this slice dispatches the tasks and
    records the plan↔runs mapping."""
    runs: dict[str, int] = {}
    for task in plan.tasks:
        run_id = delegation.spawn_peer(project, task.title or task.id, task.role, tenant=tenant)
        runs[task.id] = run_id
        msg_id = state_service.add_space_message(
            space_id,
            "system",
            f"→ {task.id} · {task.title} ({task.role}) · run #{run_id}",
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


def launch_mission(goal: str, project: str, tenant: str = "default") -> dict:
    """Decompose `goal`, post the plan, and SPAWN each task as a background run.
    Returns `{plan, space_id, runs}` (runs = task_id → run_id)."""
    plan, space_id = _decompose_and_post(goal, project, tenant)
    runs = spawn_plan(plan, project, space_id, tenant)
    return {"plan": plan.to_dict(), "space_id": space_id, "runs": runs}
