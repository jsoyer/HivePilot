"""The partition contract (propose -> ratify -> dispatch PRD, Sprint 1,
spec section 3): Pydantic models for a `partition_version: 1` JSON document
-- one work item split into N budgeted, DAG-ordered tasks.

This module is CONTRACT ONLY -- parsing + structural/semantic validation of
a partition document in isolation. It deliberately does NOT resolve a
task's `project`/`pipeline` names against live config
(`projects.yaml`/`pipelines.yaml`/`policies.yaml`), nor does it check outward
-action policy or budget ceilings against `AutopilotPolicy` -- those
referential/policy checks must run against LIVE config, never the proposal
itself (so the JSON box in the ratification UI is never a privilege
-escalation surface), and are Sprint 2's `hivepilot.services.partition_
service` concern (spec section 5). This module has ZERO optional
dependencies and ZERO side effects: `import hivepilot.partition` succeeds
standalone, with no config load, no DB access, no network.

Fail-closed discipline (this repo's documented recurring bug class -- see
`hivepilot.forges.provider.resolve_forge`'s own docstring for the sibling
instance of this same discipline): every validator here treats missing,
empty, blank, zero, or negative as REJECT, never as "unconstrained".

Named errors, not a ValidationError blob
-----------------------------------------
Every rejection raises one of the `PartitionError` subclasses below.
These are deliberately NOT `ValueError`/`TypeError`/`AssertionError`
subclasses: pydantic v2 only intercepts those three exception types when
raised inside a validator and wraps them into its own `pydantic.
ValidationError`; any other exception type raised from a validator
propagates untouched. Raising these directly is what lets a caller (Sprint
2's ratification gate, the CLI) catch e.g. `DependencyCycleError`
specifically and present a precise, actionable message, instead of parsing
a nested arbitrary pydantic error tree.

Budgets
-------
- `wall_clock_seconds` -- **mandatory**, the enforcement ceiling. Maps to
  `TaskStep.timeout_seconds` (`hivepilot/models.py`) -- the only budget the
  engine can hard-enforce.
- `cost_usd` -- **mandatory**, *admission control only*. The daily ceiling
  check (partition-sum against remaining `budget_daily_usd`) is a soft
  pre-check performed by Sprint 2's ratification gate against LIVE policy,
  never a reservation: within one dispatch wave there is no lock on spend,
  so the ceiling can be overshot by up to one wave's declared budget. This
  module only enforces that `cost_usd` is present and positive -- it does
  not itself check it against any ceiling.
- Tokens are deliberately NOT a budget unit: unenforceable across
  `ansible`/`kubectl`/`helm`/`shell`/`container` runners (a Claude-shaped
  leak into a generic contract). A `tokens` key inside a budget object is
  rejected outright, not silently ignored.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PARTITION_VERSION = 1

_PROJECT_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$")

OnTaskFailure = Literal["continue", "halt"]


# ---------------------------------------------------------------------------
# Named errors
# ---------------------------------------------------------------------------


class PartitionError(Exception):
    """Base class for every partition-contract violation raised by this
    module. Never raised directly -- always one of the subclasses below."""


class MalformedPartitionError(PartitionError):
    """The input isn't valid JSON, or doesn't match the partition schema:
    wrong top-level type, missing required keys, an unsupported
    `partition_version`, a non-object task entry, a blank required string
    field other than `prompt` (which has its own `BlankPromptError`), or an
    empty `tasks` list."""


class BlankPromptError(PartitionError):
    """A task's `prompt` is missing, empty, or whitespace-only."""


class InvalidBudgetError(PartitionError):
    """A task's `budget` is missing entirely, or `wall_clock_seconds`/
    `cost_usd` is missing, zero, negative, non-numeric, or a `tokens` key
    was present (tokens are deliberately not a supported budget unit)."""


class DuplicateTaskIdError(PartitionError):
    """Two or more tasks in the same partition share an `id`."""


class UnknownTaskDependencyError(PartitionError):
    """A task's `depends_on` names an id that isn't declared anywhere in
    the partition."""


class DependencyCycleError(PartitionError):
    """`depends_on` edges form a cycle -- no valid topological order (wave
    plan) exists for this partition."""


class InvalidProjectTargetError(PartitionError):
    """A task's `project` doesn't match `<project>` or `<project>/
    <module>` (see `hivepilot.services.project_service.
    resolve_project_target`, which resolves this shape against live config
    -- a Sprint 2 concern; this module only validates the string shape)."""


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class SourceRef(BaseModel):
    """Where the partition's originating work item came from -- an opaque
    provenance record. Re-fetching the referenced content is
    `hivepilot.partition_sources`'s job, not this model's."""

    kind: str
    ref: str
    digest: str | None = None

    @field_validator("kind", "ref", mode="before")
    @classmethod
    def _reject_blank(cls, v: Any, info: Any) -> Any:
        if not isinstance(v, str) or not v.strip():
            raise MalformedPartitionError(
                f"source.{info.field_name} must be a non-blank string, got {v!r}"
            )
        return v


class ProposerInfo(BaseModel):
    """Which config-owned role/pipeline proposed this partition, and from
    which run -- audit provenance only; this module never authenticates or
    authorizes the proposer, it just records what it claims."""

    role: str
    pipeline: str
    run_id: int | None = None
    generated_at: str | None = None

    @field_validator("role", "pipeline", mode="before")
    @classmethod
    def _reject_blank(cls, v: Any, info: Any) -> Any:
        if not isinstance(v, str) or not v.strip():
            raise MalformedPartitionError(
                f"proposer.{info.field_name} must be a non-blank string, got {v!r}"
            )
        return v


class PartitionPolicy(BaseModel):
    """Dispatch-shape knobs -- concurrency cap and failure propagation.
    Distinct from Sprint 2's `AutopilotPolicy` (which is live, engine-owned
    config); this is proposal-declared and re-checked against live policy
    at ratify time (spec section 5.3)."""

    max_parallel: int = 1
    on_task_failure: OnTaskFailure = "continue"

    @field_validator("max_parallel")
    @classmethod
    def _positive_max_parallel(cls, v: int) -> int:
        if v < 1:
            raise MalformedPartitionError(f"policy.max_parallel must be >= 1, got {v}")
        return v


class TaskBudget(BaseModel):
    """The two enforceable budget units. Deliberately has no `tokens`
    field -- see this module's docstring."""

    wall_clock_seconds: int
    cost_usd: float

    @model_validator(mode="before")
    @classmethod
    def _validate_budget(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise InvalidBudgetError(f"task budget must be an object, got {data!r}")
        if "tokens" in data:
            raise InvalidBudgetError(
                "budget.tokens is not a supported budget unit -- tokens are "
                "unenforceable across ansible/kubectl/helm/shell/container runners; "
                "only wall_clock_seconds and cost_usd are enforceable budgets"
            )
        wall = data.get("wall_clock_seconds")
        if wall is None:
            raise InvalidBudgetError("budget.wall_clock_seconds is required")
        if not isinstance(wall, int) or isinstance(wall, bool) or wall <= 0:
            raise InvalidBudgetError(
                f"budget.wall_clock_seconds must be a positive integer, got {wall!r}"
            )
        cost = data.get("cost_usd")
        if cost is None:
            raise InvalidBudgetError("budget.cost_usd is required")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            raise InvalidBudgetError(f"budget.cost_usd must be a positive number, got {cost!r}")
        return {"wall_clock_seconds": wall, "cost_usd": float(cost)}


class Task(BaseModel):
    """A single budgeted, DAG-positioned unit of work within a partition."""

    id: str
    title: str
    project: str
    pipeline: str
    prompt: str
    depends_on: list[str] = Field(default_factory=list)
    budget: TaskBudget
    done_when: list[str] = Field(default_factory=list)
    outward: bool = False

    @model_validator(mode="before")
    @classmethod
    def _fill_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise MalformedPartitionError(f"task must be an object, got {data!r}")
        data = dict(data)
        # Force budget through TaskBudget's own validator even when the key
        # is absent entirely, so "missing budget" and "missing budget field"
        # raise the identical named InvalidBudgetError.
        data.setdefault("budget", {})
        return data

    @field_validator("id", "title", "project", "pipeline", mode="before")
    @classmethod
    def _reject_blank_required(cls, v: Any, info: Any) -> Any:
        if not isinstance(v, str) or not v.strip():
            raise MalformedPartitionError(
                f"task.{info.field_name} must be a non-blank string, got {v!r}"
            )
        return v

    @field_validator("prompt", mode="before")
    @classmethod
    def _reject_blank_prompt(cls, v: Any) -> Any:
        if not isinstance(v, str) or not v.strip():
            raise BlankPromptError(f"task.prompt must not be blank/whitespace-only, got {v!r}")
        return v

    @field_validator("project")
    @classmethod
    def _validate_project_shape(cls, v: str) -> str:
        if not _PROJECT_TARGET_RE.match(v):
            raise InvalidProjectTargetError(
                f"task.project {v!r} must match '<project>' or '<project>/<module>'"
            )
        return v

    @field_validator("depends_on", mode="before")
    @classmethod
    def _validate_depends_on(cls, v: Any) -> Any:
        if v is None:
            return []
        if not isinstance(v, list) or not all(isinstance(item, str) and item.strip() for item in v):
            raise MalformedPartitionError(
                f"task.depends_on must be a list of non-blank task ids, got {v!r}"
            )
        return v

    @field_validator("done_when", mode="before")
    @classmethod
    def _validate_done_when(cls, v: Any) -> Any:
        if v is None:
            return []
        if not isinstance(v, list) or not all(isinstance(item, str) for item in v):
            raise MalformedPartitionError(f"task.done_when must be a list of strings, got {v!r}")
        return v


class PartitionPlan(BaseModel):
    """The full `partition_version: 1` document."""

    partition_version: Literal[1]
    source: SourceRef
    proposer: ProposerInfo
    policy: PartitionPolicy = Field(default_factory=PartitionPolicy)
    tasks: list[Task]

    @model_validator(mode="before")
    @classmethod
    def _validate_top_level(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise MalformedPartitionError(
                f"partition document must be a JSON object, got {type(data).__name__}"
            )
        for required_key in ("partition_version", "source", "proposer", "tasks"):
            if required_key not in data:
                raise MalformedPartitionError(f"partition document is missing '{required_key}'")
        return data

    @field_validator("partition_version", mode="before")
    @classmethod
    def _validate_version(cls, v: Any) -> Any:
        if v != PARTITION_VERSION:
            raise MalformedPartitionError(
                f"unsupported partition_version {v!r}; only {PARTITION_VERSION} is known"
            )
        return v

    @field_validator("tasks", mode="before")
    @classmethod
    def _validate_tasks_shape(cls, v: Any) -> Any:
        if not isinstance(v, list) or not v:
            raise MalformedPartitionError("partition must declare a non-empty list of tasks")
        return v

    @model_validator(mode="after")
    def _validate_dag(self) -> "PartitionPlan":
        ids = [t.id for t in self.tasks]
        seen: set[str] = set()
        duplicates: set[str] = set()
        for task_id in ids:
            if task_id in seen:
                duplicates.add(task_id)
            seen.add(task_id)
        if duplicates:
            raise DuplicateTaskIdError(f"duplicate task id(s): {sorted(duplicates)}")

        id_set = set(ids)
        for task in self.tasks:
            unknown = sorted(dep for dep in task.depends_on if dep not in id_set)
            if unknown:
                raise UnknownTaskDependencyError(
                    f"task {task.id!r} depends_on unknown id(s): {unknown}"
                )

        _assert_acyclic({task.id: task.depends_on for task in self.tasks})
        return self


def _assert_acyclic(graph: dict[str, list[str]]) -> None:
    """Raise `DependencyCycleError` if *graph* (id -> depends_on ids)
    contains a cycle. Classic 3-color DFS; ids not present as keys are
    assumed already validated as known (see `_validate_dag` above, which
    calls this only after the unknown-dependency check has passed)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph[node]:
            if color[dep] == GRAY:
                cycle_start = stack.index(dep)
                cycle = [*stack[cycle_start:], dep]
                raise DependencyCycleError(f"depends_on cycle detected: {' -> '.join(cycle)}")
            if color[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            visit(node, [])


def parse_partition(data: Any) -> PartitionPlan:
    """Validate an already-parsed value (typically a `dict`, e.g. an
    operator-edited plan re-submitted as JSON) against the partition
    contract. Raises a `PartitionError` subclass on any violation --
    pydantic's own `ValidationError` is caught here and translated to
    `MalformedPartitionError` for the (rare) schema violation not already
    covered by one of this module's own before/after validators (e.g. an
    extra top-level field type mismatch)."""
    try:
        return PartitionPlan.model_validate(data)
    except ValidationError as exc:
        raise MalformedPartitionError(f"partition schema validation failed: {exc}") from exc


def load_partition(raw: str | bytes) -> PartitionPlan:
    """Parse and validate a raw JSON partition document. The single public
    entry point for loading a partition from untrusted input (a proposer's
    CLI submission, an operator's edited-JSON ratify request)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise MalformedPartitionError(f"partition JSON is malformed: {exc}") from exc
    return parse_partition(data)
