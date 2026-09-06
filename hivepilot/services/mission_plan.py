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

#: Execution/merge strategies (HP-69). Each name resolves to a `StrategyPreset`
#: that PRECONFIGURES TOGETHER the three knobs the mockup ties together: which
#: stages a coder task expands into, how tasks dispatch, and how they merge.
STRATEGIES = (
    "sequential",  # one pipeline, stages in sequence (in this mission)
    "new_mission",  # spawn as a separate mission/run
    "pipeline",  # full code → review → merge per task, parallel worktrees
    "code_only_self_merge",  # code-only, each coder merges its own branch
    "code_only_final_merge",  # code-only, one final merge (a Merger)
)
DEFAULT_STRATEGY = "pipeline"

#: The three merge policies the strategies choose between.
#:   per_task    — each task runs its own code→review→merge pipeline
#:   per_branch  — each coder merges its own branch
#:   final       — a single Merger merges everything at the end
#:   none        — nothing merges (preview / sequential-in-mission handoff)
MERGE_POLICIES = ("per_task", "per_branch", "final", "none")

#: The single coder role a final-merge strategy appends as the Merger.
MERGER_ROLE = "reviewer"


@dataclass(frozen=True)
class StrategyPreset:
    """A decomposition strategy as a set of PRESETS over existing machinery
    (pipelines, partitions, worktree/merge) — not a new engine. `guarantee` is
    the i18n key for the mockup's reassurance line (e.g. "+6 min/task, for the
    night")."""

    name: str
    stages: tuple[str, ...]  # e.g. ("code", "review", "merge") vs ("code",)
    dispatch: str  # "sequential" (ordered, one pipeline) | "parallel" (worktrees)
    merge: str  # one of MERGE_POLICIES
    new_mission: bool  # spawn as a separate mission/run instead of in-place
    guarantee: str  # i18n key for the mockup's guarantee label

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stages": list(self.stages),
            "dispatch": self.dispatch,
            "merge": self.merge,
            "new_mission": self.new_mission,
            "guarantee": self.guarantee,
        }


#: The mockup's five mode cards, as presets. The UI renders these directly
#: (label + guarantee); `spawn_plan` reads dispatch/merge to wire behavior.
STRATEGY_PRESETS: dict[str, StrategyPreset] = {
    "sequential": StrategyPreset(
        name="sequential",
        stages=("code",),
        dispatch="sequential",
        merge="final",
        new_mission=False,
        guarantee="strategy.guarantee.sequential",
    ),
    "new_mission": StrategyPreset(
        name="new_mission",
        stages=("code",),
        dispatch="sequential",
        merge="final",
        new_mission=True,
        guarantee="strategy.guarantee.newMission",
    ),
    "pipeline": StrategyPreset(
        name="pipeline",
        stages=("code", "review", "merge"),
        dispatch="parallel",
        merge="per_task",
        new_mission=False,
        guarantee="strategy.guarantee.pipeline",
    ),
    "code_only_self_merge": StrategyPreset(
        name="code_only_self_merge",
        stages=("code",),
        dispatch="parallel",
        merge="per_branch",
        new_mission=False,
        guarantee="strategy.guarantee.codeSelfMerge",
    ),
    "code_only_final_merge": StrategyPreset(
        name="code_only_final_merge",
        stages=("code",),
        dispatch="parallel",
        merge="final",
        new_mission=False,
        guarantee="strategy.guarantee.codeFinalMerge",
    ),
}


def resolve_strategy(name: str | None) -> StrategyPreset:
    """Map a strategy NAME to its `StrategyPreset`, falling back to the default
    preset for an unknown/None name (never raises)."""
    return STRATEGY_PRESETS.get(name or "", STRATEGY_PRESETS[DEFAULT_STRATEGY])


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
            # The resolved preset so the UI can render the mode card (stages,
            # dispatch, merge policy, guarantee label) without re-deriving it.
            "strategy_detail": resolve_strategy(self.strategy).to_dict(),
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
