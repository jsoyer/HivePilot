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

from hivepilot.services import events, mission_plan, state_service


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


def decompose_feature(goal: str, project: str, tenant: str = "default") -> dict:
    """Decompose `goal` into a `MissionPlan`, post the breakdown into the
    project's Orchestrateur Espace (with a per-task action trace), and return
    `{plan, space_id}`. The actual spawning of the tasks lands in a later HP-49
    slice; this produces and surfaces the plan."""
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
    return {"plan": plan.to_dict(), "space_id": space_id}
