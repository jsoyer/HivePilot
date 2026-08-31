"""MissionPlan — the structured output of the orchestrator's decomposition
(HP-49, Cycle 1 · P2).

When the orchestrator breaks a feature into a Kanban, it produces a `MissionPlan`:
an ordered set of `MissionTask`s (each assigned to a role, with dependencies)
plus two configuration surfaces that later issues fill in:

- `strategy` — how the tasks run + merge (HP-69: sequential / pipeline / code-only …).
- `roles_config` — per-role model + repli (fallback) for THIS mission (HP-70).

Decomposition itself is pluggable: `register_planner` installs the real
(LLM-backed) planner; with none registered `decompose` returns an honest
single-task fallback rather than inventing a breakdown. So the plan model, its
serialization, and the config hooks are here and fully testable now; the
intelligence plugs in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: Execution/merge strategies (HP-69 wires each to pipelines/partitions/merge).
STRATEGIES = (
    "sequential",  # one pipeline, stages in sequence
    "new_mission",  # spawn as a separate mission/run
    "pipeline",  # full code → review → merge per task, parallel worktrees
    "code_only_self_merge",  # code-only, each coder merges its own branch
    "code_only_final_merge",  # code-only, one final merge
)
DEFAULT_STRATEGY = "pipeline"

#: The role a fallback single-task plan is assigned to (the code default role).
DEFAULT_TASK_ROLE = "developer"


@dataclass
class MissionTask:
    id: str
    title: str
    role: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "role": self.role,
            "description": self.description,
            "depends_on": list(self.depends_on),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MissionTask":
        return MissionTask(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            role=str(data.get("role", DEFAULT_TASK_ROLE)),
            description=str(data.get("description", "")),
            depends_on=[str(d) for d in (data.get("depends_on") or [])],
        )


@dataclass
class MissionPlan:
    goal: str
    tasks: list[MissionTask] = field(default_factory=list)
    #: HP-69 hook — how the tasks execute + merge. One of STRATEGIES.
    strategy: str = DEFAULT_STRATEGY
    #: HP-70 hook — per-role {"model": ..., "repli": ...} override for THIS
    #: mission only (does not touch roles.yaml). Empty = use the role defaults.
    roles_config: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "strategy": self.strategy,
            "tasks": [t.to_dict() for t in self.tasks],
            "roles_config": self.roles_config,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MissionPlan":
        strategy = str(data.get("strategy", DEFAULT_STRATEGY))
        if strategy not in STRATEGIES:
            strategy = DEFAULT_STRATEGY
        return MissionPlan(
            goal=str(data.get("goal", "")),
            tasks=[MissionTask.from_dict(t) for t in (data.get("tasks") or [])],
            strategy=strategy,
            roles_config=dict(data.get("roles_config") or {}),
        )


#: (goal, project|None) -> MissionPlan. Installed by the orchestrator (HP-49).
Planner = Callable[[str, "str | None"], MissionPlan]

_planner: Planner | None = None


def register_planner(fn: Planner | None) -> None:
    global _planner
    _planner = fn


def _fallback_plan(goal: str) -> MissionPlan:
    """Honest no-LLM decomposition: a single task carrying the whole goal,
    rather than an invented breakdown."""
    title = goal.strip().splitlines()[0][:80] if goal.strip() else "Mission"
    return MissionPlan(
        goal=goal,
        tasks=[MissionTask(id="t1", title=title, role=DEFAULT_TASK_ROLE, description=goal)],
        strategy=DEFAULT_STRATEGY,
    )


def decompose(
    goal: str, project: str | None = None, *, planner: Planner | None = None
) -> MissionPlan:
    """Break `goal` into a `MissionPlan`. Uses the given/registered planner when
    available, else the honest single-task fallback. Fail-safe: a planner that
    raises (or returns something invalid) degrades to the fallback, never
    crashes the caller."""
    active = planner or _planner
    if active is None:
        return _fallback_plan(goal)
    try:
        plan = active(goal, project)
    except Exception as exc:  # noqa: BLE001 — a broken planner degrades, never crashes
        logger.warning("mission_plan.planner_failed", error=str(exc))
        return _fallback_plan(goal)
    if not isinstance(plan, MissionPlan) or not plan.tasks:
        return _fallback_plan(goal)
    if plan.strategy not in STRATEGIES:
        plan.strategy = DEFAULT_STRATEGY
    return plan
