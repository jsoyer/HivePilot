"""The partition journal + the ratification gate (propose -> ratify ->
dispatch PRD, Sprint 2 -- spec sections 5, 6 and 8).

What this module owns
---------------------
1. **The journal** (spec section 8): persistence for the two new tables
   (`partitions`, `partition_tasks`, created by `state_service.init_db`) and
   the full state machine over them. `runs`/`steps`/`interactions` already
   give HivePilot durable execution history; what was genuinely absent is
   the `(partition, task) -> run` mapping, a claim lifecycle over it, the
   outward-consent record, and the PR link.
2. **The ratification gate** (spec section 5): the fail-closed, ordered
   validation an operator-edited plan must pass before a single task is
   dispatched.
3. **Outward consent** (spec section 6): a DISTINCT axis from
   `is_destructive`. `is_destructive` asks "could this damage the target
   system", is per-step, and is resolved by the runner at execution time.
   Outward asks "may this become visible outside this machine", is
   per-dispatch, and is resolved by the operator at ratification.
   `terraform apply` is destructive and not outward; `gh pr create` is
   outward and not destructive. Two independent booleans; both must be
   satisfied.

Dispatch itself (the wave planner, the `autopilot_queue` writes, the
startup reconciler) is Sprint 3 and deliberately absent here -- but note
that NOTHING in this module can dispatch: `ratify_partition` only ever
moves a partition from `proposed` to `ratified` and writes `pending`
journal rows. A partition never dispatches without human ratification.

One state-transition mechanism, not two
---------------------------------------
Every transition below is a conditional
``UPDATE ... WHERE <key> AND status=? [AND claimed_by=?]`` whose
``rowcount == 1`` is the caller's answer to "did I win?" -- literally the
shape of `state_service.claim_swarm_event` / `mark_swarm_event_running`,
with the same `db.ph()` portability. There is no read-then-act check
anywhere in this module, because a read-then-act check is a TOCTOU race
(see `mark_swarm_event_running`'s docstring for the incident that
established this rule here).

Why not reuse what already exists
----------------------------------
- `autopilot_gate` is NOT reused: its condition (b) requires
  ``require_approval == False``, the exact inverse of a human-ratified
  plan. Weakening it, or adding a bypass flag, would open a fail-open hole
  in the most carefully fail-closed module in the repo. The queue TABLE,
  its atomic `_claim_running`, and its pause/stop kill switch ARE reused
  (Sprint 3) -- the gate is not.
- The `approvals` table is NOT reused: it is ``PRIMARY KEY(run_id)``, and a
  partition PRECEDES and SPANS N runs. There is no run id to key it by at
  ratification time. Hence `partitions`/`partition_tasks`.

Fail-closed discipline
----------------------
This repo has a documented recurring bug class where an empty/absent value
on a gate is read as "no constraint" and the gate passes. Every gate below
is written the other way round: an absent `outward_actions` allowlist, an
absent cost ceiling, an absent wall-clock ceiling, an unresolvable pipeline
config, an unavailable spend figure and an absent `expected_digest` all
DENY.
"""

from __future__ import annotations

import difflib
import json
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from hivepilot.config import settings
from hivepilot.partition import DependencyCycleError, PartitionError, PartitionPlan, load_partition
from hivepilot.partition_sources import compute_digest
from hivepilot.services import autopilot_queue, db, project_service, state_service
from hivepilot.services.autopilot_policy import AutopilotPolicy, get_autopilot_policy
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

PARTITION_STATUSES = frozenset(
    {"proposed", "ratified", "dispatching", "completed", "failed", "vetoed", "expired"}
)
TASK_STATUSES = frozenset(
    {"pending", "claimed", "running", "committed", "failed", "skipped", "cancelled"}
)


# ---------------------------------------------------------------------------
# Errors -- each carries the HTTP status Sprint 3's API layer must translate
# it into, so the mapping is defined ONCE, next to the rule it belongs to,
# rather than re-derived by every caller (CLI, API, chatops).
# ---------------------------------------------------------------------------


class RatificationError(Exception):
    """Base class for every ratification refusal. Never raised directly."""

    status_code: int = 400
    code: str = "invalid"


class PartitionNotFoundError(RatificationError):
    """No `partitions` row with that id in the caller's tenant. Deliberately
    indistinguishable from a cross-tenant id: a 404 must never confirm the
    existence of another tenant's partition."""

    status_code = 404
    code = "not_found"


class MalformedPlanError(RatificationError):
    """Step 1: the submitted JSON isn't a valid `partition_version: 1`
    document. Nothing is dispatched."""

    status_code = 400
    code = "malformed"


class ReferentialError(RatificationError):
    """Step 2: the plan is well-formed but names something that doesn't
    exist in LIVE config (an unknown pipeline, an unresolvable project or
    module), or carries a prompt that fails the shared prompt validation."""

    status_code = 400
    code = "referential"


class PolicyDeniedError(RatificationError):
    """Step 3: the plan is well-formed and referentially valid, but LIVE
    policy refuses it -- an outward action outside the project's allowlist,
    an unconditionally-refused `merge_pr`, or a budget/wall-clock ceiling
    exceeded (or not configured at all, which denies)."""

    status_code = 403
    code = "policy_denied"


class ConsentRequiredError(RatificationError):
    """Step 4: the computed outward set is non-empty and `outward_consent`
    is False. `actions` names the EXACT actions that require consent, so the
    operator is never asked to consent to an unnamed "something outward"."""

    status_code = 403
    code = "consent_required"

    def __init__(self, message: str, *, actions: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.actions = actions


class DigestMismatchError(RatificationError):
    """Step 5: `expected_digest` doesn't match the stored `proposed_digest`
    -- a stale browser tab trying to ratify a superseded plan."""

    status_code = 409
    code = "digest_mismatch"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutwardAssessment:
    """The result of resolving a plan's outward footprint against LIVE
    config (never against the proposal's own `outward` flags -- those are
    proposal-declared and therefore untrusted)."""

    actions: frozenset[str]
    per_task: dict[str, frozenset[str]]
    total_cost_usd: float


@dataclass(frozen=True)
class RatifyOutcome:
    """The outcome of a `ratify_partition` call.

    `idempotent=True` means this call LOST the conditional UPDATE (the
    partition was no longer `proposed`) and therefore changed NOTHING and
    dispatched NOTHING -- a second ratify is a no-op, never a second
    dispatch.
    """

    partition_id: str
    status: str
    ratified_digest: str
    outward_actions: tuple[str, ...]
    outward_consent: bool
    diff: str
    task_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    idempotent: bool = False


# ---------------------------------------------------------------------------
# Digests + diff
# ---------------------------------------------------------------------------


def _canonical(text: str) -> str:
    """Canonical JSON rendering of *text* (sorted keys, 2-space indent).

    Used for BOTH the digest and the audit diff so that a whitespace-only
    reformat of an otherwise identical plan neither invalidates a browser
    tab's `expected_digest` nor shows up as a fabricated "edit" in the audit
    trail. Falls back to the raw text when it doesn't parse -- callers have
    already rejected unparseable input by then, so this only guards direct
    misuse of the helper.
    """
    try:
        return json.dumps(json.loads(text), sort_keys=True, indent=2)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text


def partition_digest(text: str) -> str:
    """The `sha256:<hex>` digest of a partition document.

    Reuses `hivepilot.partition_sources.compute_digest` -- the one canonical
    digest computation the four built-in sources already share -- so a
    partition digest and a source digest can never drift to different hash
    shapes.
    """
    return compute_digest(_canonical(text))


def plan_diff(proposed_json: str, ratified_json: str) -> str:
    """A unified diff between the proposed and the ratified plan.

    This is the audit artifact that makes "edit-then-approve" accountable:
    without it, an operator could silently rewrite a plan and the journal
    would only ever show the final version.
    """
    return "\n".join(
        difflib.unified_diff(
            _canonical(proposed_json).splitlines(),
            _canonical(ratified_json).splitlines(),
            fromfile="proposed",
            tofile="ratified",
            lineterm="",
        )
    )


# ---------------------------------------------------------------------------
# Journal -- partitions
# ---------------------------------------------------------------------------


def create_partition(
    *,
    plan_json: str,
    tenant: str = "default",
    proposer_run_id: int | None = None,
    partition_id: str | None = None,
) -> str:
    """Persist a newly PROPOSED partition and return its id.

    The document must at minimum parse against the partition contract
    (`hivepilot.partition`) -- a proposer may not park unparseable garbage
    in the operator's queue. Referential/policy validation is deliberately
    NOT done here: it must run against LIVE config at RATIFY time (spec
    section 5.3), because config can change between propose and ratify and
    because the operator may edit the plan in between.

    The raw text is stored verbatim (never a re-serialized round-trip), so
    what the operator reviews is exactly what the proposer emitted.
    """
    plan = load_partition(plan_json)
    new_id = partition_id or uuid.uuid4().hex
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO partitions (id, tenant, source_kind, source_ref, source_digest, "
                "proposer_run_id, proposed_json, proposed_digest, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed')"
            ),
            (
                new_id,
                tenant,
                plan.source.kind,
                plan.source.ref,
                plan.source.digest,
                proposer_run_id if proposer_run_id is not None else plan.proposer.run_id,
                plan_json,
                partition_digest(plan_json),
            ),
        )
    logger.info(
        "partition.created",
        partition_id=new_id,
        tenant=tenant,
        tasks=len(plan.tasks),
        source_kind=plan.source.kind,
    )
    return new_id


def get_partition(partition_id: str, *, tenant: str | None = "default") -> dict[str, Any] | None:
    """Return the `partitions` row, or `None`.

    `tenant=None` means "any tenant" and must only ever be used by
    admin/maintenance code paths -- every operator-facing caller passes its
    own tenant, so a cross-tenant id is indistinguishable from a missing one.
    """
    state_service.init_db()
    with db.connect() as conn:
        if tenant is None:
            row = conn.execute(
                db.ph("SELECT * FROM partitions WHERE id=?"), (partition_id,)
            ).fetchone()
        else:
            row = conn.execute(
                db.ph("SELECT * FROM partitions WHERE id=? AND tenant=?"), (partition_id, tenant)
            ).fetchone()
    return dict(row) if row else None


def list_partitions(
    *, tenant: str | None = "default", status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List partitions, newest first. `tenant=None` means "all tenants"
    (never the implicit default) -- the same tenant-scoping convention as
    `autopilot_queue.list_queue`/`state_service.list_recent_runs`."""
    state_service.init_db()
    sql = "SELECT * FROM partitions WHERE 1=1"
    params: list[Any] = []
    if tenant is not None:
        sql += " AND tenant=?"
        params.append(tenant)
    if status is not None:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [dict(row) for row in rows]


def list_partition_tasks(partition_id: str) -> list[dict[str, Any]]:
    """The journal rows for *partition_id*, in stable task-id order."""
    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph("SELECT * FROM partition_tasks WHERE partition_id=? ORDER BY task_id ASC"),
            (partition_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Journal -- partition state transitions (conditional UPDATE, rowcount == 1)
# ---------------------------------------------------------------------------


def _transition_partition(partition_id: str, *, from_status: str, to_status: str) -> bool:
    if to_status not in PARTITION_STATUSES:
        raise ValueError(f"Invalid partition status: {to_status!r}")
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partitions SET status=?, updated_ts=CURRENT_TIMESTAMP "
                "WHERE id=? AND status=?"
            ),
            (to_status, partition_id, from_status),
        )
        won = cur.rowcount == 1
    logger.info(
        "partition.transition",
        partition_id=partition_id,
        from_status=from_status,
        to_status=to_status,
        won=won,
    )
    return won


def mark_partition_dispatching(partition_id: str) -> bool:
    """`ratified -> dispatching`. Returns False (no-op) from ANY other
    status -- in particular from `proposed`, which is the persistence-level
    half of "a partition never dispatches without human ratification"."""
    return _transition_partition(partition_id, from_status="ratified", to_status="dispatching")


def mark_partition_completed(partition_id: str) -> bool:
    """`dispatching -> completed`. Only ever called EXPLICITLY by the
    dispatcher once every task reached a terminal state -- `dispatching`
    never auto-completes (spec section 8), because "nothing is running any
    more" and "the work finished" are different claims."""
    return _transition_partition(partition_id, from_status="dispatching", to_status="completed")


def mark_partition_failed(partition_id: str) -> bool:
    """`dispatching -> failed`."""
    return _transition_partition(partition_id, from_status="dispatching", to_status="failed")


def veto_partition(partition_id: str, *, actor: str) -> bool:
    """`proposed -> vetoed` -- an explicit human refusal. Never applies to a
    partition already ratified: a ratified partition is immutable."""
    won = _transition_partition(partition_id, from_status="proposed", to_status="vetoed")
    if won:
        state_service.record_interaction(
            actor=actor,
            action="partition.veto",
            target=partition_id,
            summary=f"Partition {partition_id} vetoed by {actor}.",
            metadata={"partition_id": partition_id},
        )
    return won


def expire_partition(partition_id: str) -> bool:
    """`proposed -> expired`. A ratified partition can never expire."""
    return _transition_partition(partition_id, from_status="proposed", to_status="expired")


# ---------------------------------------------------------------------------
# Journal -- task lifecycle (conditional UPDATE, rowcount == 1)
# ---------------------------------------------------------------------------


def insert_task_rows(partition_id: str, plan: PartitionPlan) -> tuple[str, ...]:
    """Write one `pending` journal row per task, idempotently.

    `ON CONFLICT ... DO NOTHING` (rather than SQLite-only `INSERT OR
    IGNORE`) so this stays portable to the optional Postgres backend, and so
    a retried ratify can never reset an already-progressing task row back to
    `pending`.
    """
    state_service.init_db()
    with db.connect() as conn:
        for task in plan.tasks:
            conn.execute(
                db.ph(
                    "INSERT INTO partition_tasks "
                    "(partition_id, task_id, status, attempt, wall_clock_seconds) "
                    "VALUES (?, ?, 'pending', 0, ?) "
                    "ON CONFLICT(partition_id, task_id) DO NOTHING"
                ),
                (partition_id, task.id, task.budget.wall_clock_seconds),
            )
    return tuple(task.id for task in plan.tasks)


def claim_task(partition_id: str, task_id: str, *, claimed_by: str) -> bool:
    """`pending -> claimed` for *claimed_by*. Returns True iff THIS call won.

    Claim BEFORE creating the run row, so a crash in between leaves a
    visible `claimed` row with no `run_id` -- recoverable by the reconciler
    (`release_stale_claim`), never a double dispatch.
    """
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='claimed', claimed_by=?, "
                "claimed_at=CURRENT_TIMESTAMP, updated_ts=CURRENT_TIMESTAMP "
                "WHERE partition_id=? AND task_id=? AND status='pending'"
            ),
            (claimed_by, partition_id, task_id),
        )
        won = cur.rowcount == 1
    logger.info(
        "partition.task_claim",
        partition_id=partition_id,
        task_id=task_id,
        claimed_by=claimed_by,
        claimed=won,
    )
    return won


def mark_task_running(
    partition_id: str,
    task_id: str,
    *,
    claimed_by: str,
    run_id: int | None = None,
    queue_id: int | None = None,
) -> bool:
    """`claimed -> running`, but ONLY when the row is claimed BY
    *claimed_by*.

    The `AND claimed_by=?` half is not decoration: without it a claim
    race-LOSER could still read `status='claimed'` and start the task too --
    the exact TOCTOU hole `mark_swarm_event_running` was written to close.
    """
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='running', run_id=?, queue_id=?, "
                "updated_ts=CURRENT_TIMESTAMP "
                "WHERE partition_id=? AND task_id=? AND status='claimed' AND claimed_by=?"
            ),
            (run_id, queue_id, partition_id, task_id, claimed_by),
        )
        won = cur.rowcount == 1
    logger.info(
        "partition.task_running",
        partition_id=partition_id,
        task_id=task_id,
        claimed_by=claimed_by,
        run_id=run_id,
        running=won,
    )
    return won


def mark_task_committed(
    partition_id: str,
    task_id: str,
    *,
    claimed_by: str,
    pr_url: str | None = None,
    cost_usd: float | None = None,
) -> bool:
    """`running -> committed` for the claim owner.

    `pr_url` stays NULL when the forge cannot cheaply produce one -- the
    journal then shows "—". NEVER a fabricated URL.
    """
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='committed', pr_url=?, cost_usd=?, "
                "updated_ts=CURRENT_TIMESTAMP "
                "WHERE partition_id=? AND task_id=? AND status='running' AND claimed_by=?"
            ),
            (pr_url, cost_usd, partition_id, task_id, claimed_by),
        )
        won = cur.rowcount == 1
    logger.info(
        "partition.task_committed",
        partition_id=partition_id,
        task_id=task_id,
        pr_url_recorded=pr_url is not None,
        committed=won,
    )
    return won


def mark_task_failed(
    partition_id: str, task_id: str, *, claimed_by: str, cost_usd: float | None = None
) -> bool:
    """`claimed|running -> failed` for the claim owner."""
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='failed', cost_usd=?, "
                "updated_ts=CURRENT_TIMESTAMP WHERE partition_id=? AND task_id=? "
                "AND status IN ('claimed', 'running') AND claimed_by=?"
            ),
            (cost_usd, partition_id, task_id, claimed_by),
        )
        won = cur.rowcount == 1
    logger.info("partition.task_failed", partition_id=partition_id, task_id=task_id, failed=won)
    return won


def mark_task_skipped(partition_id: str, task_id: str) -> bool:
    """`pending -> skipped` -- a task whose prerequisite failed.

    Deliberately a DIFFERENT terminal state from `failed`: running a task
    whose prerequisite failed is a correctness bug, not a policy choice, and
    recording it as `failed` would lie about what happened.
    """
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='skipped', updated_ts=CURRENT_TIMESTAMP "
                "WHERE partition_id=? AND task_id=? AND status='pending'"
            ),
            (partition_id, task_id),
        )
        won = cur.rowcount == 1
    logger.info("partition.task_skipped", partition_id=partition_id, task_id=task_id, skipped=won)
    return won


def mark_task_cancelled(partition_id: str, task_id: str) -> bool:
    """`pending|claimed -> cancelled`. A RUNNING task is never cancelled
    here: running agents are never killed, only cooperatively cancelled via
    `async_run_service.request_cancel`."""
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='cancelled', updated_ts=CURRENT_TIMESTAMP "
                "WHERE partition_id=? AND task_id=? AND status IN ('pending', 'claimed')"
            ),
            (partition_id, task_id),
        )
        won = cur.rowcount == 1
    logger.info(
        "partition.task_cancelled", partition_id=partition_id, task_id=task_id, cancelled=won
    )
    return won


def release_stale_claim(partition_id: str, task_id: str) -> bool:
    """`claimed -> pending`, but ONLY for a claim that never produced a run
    (`run_id IS NULL`).

    This is the crash-between-claim-and-create recovery primitive. The
    `AND run_id IS NULL` half is what makes it safe to run at startup: a
    task that DID reach `mark_task_running` has a `run_id` and is therefore
    never rewound into the dispatch path, so recovery can never
    double-dispatch.
    """
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partition_tasks SET status='pending', claimed_by=NULL, claimed_at=NULL, "
                "updated_ts=CURRENT_TIMESTAMP WHERE partition_id=? AND task_id=? "
                "AND status='claimed' AND run_id IS NULL"
            ),
            (partition_id, task_id),
        )
        won = cur.rowcount == 1
    logger.info(
        "partition.task_claim_released", partition_id=partition_id, task_id=task_id, released=won
    )
    return won


# ---------------------------------------------------------------------------
# The ratification gate (spec section 5) -- fail-closed, IN ORDER
# ---------------------------------------------------------------------------


def _policy_project_key(target: str) -> str:
    """The `policies.yaml` key governing a `<project>` or `<project>/<module>`
    target. An exact project match wins outright (mirroring
    `project_service.resolve_project_target`'s own resolution order); only
    then is a `/` treated as a project/module split."""
    try:
        projects = project_service.load_projects().projects
    except Exception:  # noqa: BLE001 - unreadable config is denied by the caller's own check
        projects = {}
    if target in projects:
        return target
    return target.split("/", 1)[0]


def _check_referential(plan: PartitionPlan) -> None:
    """Step 2: every pipeline exists, every project/module resolves, every
    prompt passes the shared `_validate_extra_prompt`.

    Unique ids and an acyclic `depends_on` DAG were already enforced by the
    contract itself (`hivepilot.partition`), and are therefore guaranteed
    here for free because this function is only ever reached through
    `load_partition`.
    """
    try:
        pipelines = project_service.load_pipelines().pipelines
    except Exception as exc:  # noqa: BLE001 - unreadable/malformed config denies, never allows
        raise ReferentialError(f"pipelines config could not be loaded: {exc}") from exc

    # Local import: `api_service` pulls FastAPI and the orchestrator, and
    # this module is imported by the CLI. Importing the shared validator
    # lazily keeps that cost off the import path while still guaranteeing a
    # partition prompt is validated by exactly the SAME function as a
    # `POST /v1/runs` prompt -- never a weaker reimplementation.
    from hivepilot.services.api_service import _validate_extra_prompt

    for task in plan.tasks:
        if task.pipeline not in pipelines:
            raise ReferentialError(
                f"task {task.id!r} names unknown pipeline {task.pipeline!r} "
                f"(known: {', '.join(sorted(pipelines)) or 'none'})"
            )
        try:
            project_service.resolve_project_target(task.project)
        except Exception as exc:  # noqa: BLE001 - any resolution failure is a referential denial
            raise ReferentialError(f"task {task.id!r}: {exc}") from exc
        try:
            _validate_extra_prompt(task.prompt)
        except ValueError as exc:
            raise ReferentialError(f"task {task.id!r} prompt rejected: {exc}") from exc


def assess_outward(plan: PartitionPlan) -> OutwardAssessment:
    """Resolve the plan's outward footprint from LIVE pipeline config.

    A task's own `outward: true/false` field is deliberately IGNORED here.
    It is proposal-declared -- i.e. operator-editable in the very JSON box
    this gate exists to police -- so trusting it would make that box a
    privilege-escalation surface. What a pipeline actually does is a
    property of `pipelines.yaml`/`tasks.yaml`, and that is what is read.

    Unresolvable config yields the FULL outward set for that task (see
    `autopilot_queue.pipeline_outward_actions`), so "I cannot tell" denies.
    """
    per_task = {
        task.id: autopilot_queue.pipeline_outward_actions(task.pipeline) for task in plan.tasks
    }
    union: frozenset[str] = frozenset().union(*per_task.values()) if per_task else frozenset()
    total = sum(task.budget.cost_usd for task in plan.tasks)
    return OutwardAssessment(actions=union, per_task=per_task, total_cost_usd=float(total))


def _check_policy(plan: PartitionPlan, assessment: OutwardAssessment, *, tenant: str) -> None:
    """Step 3: LIVE policy, never the proposal.

    Everything checked here is read from `policies.yaml` at call time via
    `get_autopilot_policy`, so an operator who edits the JSON box cannot
    widen what policy allows: an edit naming an out-of-policy pipeline
    denies even though the original proposal was valid.
    """
    # Resolved once per distinct project -- a partition routinely fans
    # several tasks at the same project.
    policies: dict[str, AutopilotPolicy] = {}

    def policy_for(target: str) -> AutopilotPolicy:
        key = _policy_project_key(target)
        if key not in policies:
            policies[key] = get_autopilot_policy(key)
        return policies[key]

    for task in plan.tasks:
        policy = policy_for(task.project)
        actions = assessment.per_task[task.id]

        # `merge_pr` is refused UNCONDITIONALLY -- not merely "unless
        # allowlisted". A partition inherits autopilot's never-auto-merge
        # invariant, so no allowlist entry and no ticked checkbox can ever
        # authorize it.
        if "forge_merge" in actions:
            raise PolicyDeniedError(
                f"task {task.id!r} uses pipeline {task.pipeline!r}, which would auto-merge a PR "
                "(git.merge_pr) -- refused unconditionally in a partition dispatch"
            )

        # ALLOWLIST. An absent or empty `outward_actions` yields an empty
        # allowed set, so ANY outward action denies. Unknown tokens in the
        # configured allowlist are dropped rather than honoured: a typo must
        # never authorize anything.
        allowed = frozenset(policy.outward_actions) & autopilot_queue.OUTWARD_ACTIONS
        not_allowed = actions - allowed
        if not_allowed:
            raise PolicyDeniedError(
                f"task {task.id!r} would perform outward action(s) "
                f"{sorted(not_allowed)} which are not in the outward_actions allowlist "
                f"for project {task.project!r} (allowed: {sorted(allowed) or 'none'})"
            )

        # Wall-clock ceiling. An unconfigured ceiling denies -- an
        # unenforceable budget is not a budget.
        cap = policy.max_task_wall_clock_seconds
        if cap is None:
            raise PolicyDeniedError(
                f"project {task.project!r} has no positive max_task_wall_clock_seconds "
                "configured -- no partition task may be ratified for it"
            )
        if task.budget.wall_clock_seconds > cap:
            raise PolicyDeniedError(
                f"task {task.id!r} wall_clock_seconds={task.budget.wall_clock_seconds} exceeds "
                f"max_task_wall_clock_seconds={cap} for project {task.project!r}"
            )

    # Cost admission control. The partition SUM is checked against EVERY
    # participating project's ceiling (the strictest reading), because a
    # partition spanning projects consumes all of their budgets at once and
    # there is no honest per-project split of one shared spend figure.
    #
    # Honesty clause (spec section 3): this USD ceiling is a soft
    # pre-check, not a reservation. Within one dispatch wave nothing is
    # locked, so the ceiling can be overshot by up to one wave's declared
    # budget. Documented, not implied away.
    try:
        spent = autopilot_queue.spent_today_usd(tenant=tenant)
    except Exception as exc:  # noqa: BLE001 - unknown spend denies, never allows
        raise PolicyDeniedError(
            f"daily spend could not be resolved ({exc.__class__.__name__}: {exc}) "
            "-- refusing to admit a partition against an unknown budget"
        ) from exc

    for target in sorted({task.project for task in plan.tasks}):
        policy = policy_for(target)
        partition_cap = policy.max_partition_cost_usd
        if partition_cap is None:
            raise PolicyDeniedError(
                f"project {target!r} has no positive max_partition_cost_usd configured "
                "-- no partition may be ratified for it"
            )
        daily = policy.budget_daily_usd
        if daily is None or daily <= 0:
            raise PolicyDeniedError(
                f"project {target!r} has no positive budget_daily_usd configured "
                "-- no partition may be ratified for it"
            )
        ceiling = min(partition_cap, daily - spent)
        if assessment.total_cost_usd > ceiling:
            raise PolicyDeniedError(
                f"partition cost sum ${assessment.total_cost_usd:.2f} exceeds the ceiling "
                f"${ceiling:.2f} for project {target!r} "
                f"(max_partition_cost_usd=${partition_cap:.2f}, "
                f"budget_daily_usd=${daily:.2f} - spent_today=${spent:.2f})"
            )


def _check_consent(assessment: OutwardAssessment, *, outward_consent: bool) -> None:
    """Step 4: a non-empty outward set requires explicit consent, and the
    refusal NAMES the exact actions -- an operator is never asked to consent
    to an unnamed "something outward"."""
    if assessment.actions and not outward_consent:
        named = tuple(sorted(assessment.actions))
        raise ConsentRequiredError(
            "this plan performs outward-visible action(s) "
            f"{list(named)} -- outward_consent must be explicitly granted",
            actions=named,
        )


def validate_ratification(
    plan: PartitionPlan, *, outward_consent: bool, tenant: str = "default"
) -> OutwardAssessment:
    """Steps 2-4 of the gate, in order, against LIVE config.

    Exposed separately from `ratify_partition` so a UI can dry-run the exact
    same checks (and get the exact same refusal messages) before the
    operator commits -- never a second, drifting copy of the rules.
    """
    _check_referential(plan)
    assessment = assess_outward(plan)
    _check_policy(plan, assessment, tenant=tenant)
    _check_consent(assessment, outward_consent=outward_consent)
    return assessment


def ratify_partition(
    partition_id: str,
    *,
    partition_json: str,
    outward_consent: bool,
    approver: str,
    expected_digest: str | None,
    tenant: str = "default",
) -> RatifyOutcome:
    """Ratify (possibly edited) *partition_json* for *partition_id*.

    Validation is fail-closed and IN THIS ORDER (spec section 5):

    1. Parse + model validation -- malformed denies, nothing dispatched.
    2. Referential -- pipelines/projects/modules exist, prompts validate.
    3. Policy, against LIVE config -- outward allowlist, unconditional
       `merge_pr` refusal, cost sum, wall-clock ceiling.
    4. Consent -- a non-empty outward set with `outward_consent=False`
       denies, naming the exact actions.
    5. Concurrency -- `expected_digest` must match the stored
       `proposed_digest` (a stale tab can never ratify a superseded plan).
    6. Idempotency -- the transition is a conditional UPDATE; `rowcount == 1`
       wins. A second ratify is a no-op, never a second dispatch.

    The ordering consequence is intentional: an edited plan is fully
    re-validated against live policy BEFORE the digest check, so a stale tab
    carrying an out-of-policy edit is refused on the merits (403) rather
    than merely on staleness (409).

    Returns a `RatifyOutcome`. This function NEVER dispatches anything: on
    success it moves the partition to `ratified` and writes `pending`
    journal rows. Dispatch is a separate, explicitly-invoked step.
    """
    row = get_partition(partition_id, tenant=tenant)
    if row is None:
        raise PartitionNotFoundError(f"Unknown partition {partition_id!r}.")

    # --- 1. parse + model validation --------------------------------------
    try:
        plan = load_partition(partition_json)
    except PartitionError as exc:
        _record_denial(partition_id, approver, "malformed", str(exc), outward_consent)
        raise MalformedPlanError(str(exc)) from exc

    # --- 2/3/4. referential, policy (live config), consent -----------------
    try:
        assessment = validate_ratification(plan, outward_consent=outward_consent, tenant=tenant)
    except RatificationError as exc:
        _record_denial(partition_id, approver, exc.code, str(exc), outward_consent)
        raise

    # --- 5. concurrency ----------------------------------------------------
    stored_digest = row.get("proposed_digest")
    if not expected_digest or expected_digest != stored_digest:
        _record_denial(
            partition_id,
            approver,
            "digest_mismatch",
            f"expected_digest {expected_digest!r} != stored {stored_digest!r}",
            outward_consent,
        )
        raise DigestMismatchError(
            f"partition {partition_id!r} has moved on since this plan was loaded "
            f"(expected_digest={expected_digest!r}, current={stored_digest!r}) -- reload it"
        )

    ratified_digest = partition_digest(partition_json)
    diff = plan_diff(str(row.get("proposed_json") or ""), partition_json)
    warnings = _source_digest_warnings(row, plan)

    # --- 6. idempotency: the conditional transition ------------------------
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE partitions SET status='ratified', ratified_json=?, ratified_digest=?, "
                "ratified_diff=?, outward_consent=?, ratified_by=?, "
                "ratified_at=CURRENT_TIMESTAMP, updated_ts=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='proposed'"
            ),
            (
                partition_json,
                ratified_digest,
                diff,
                int(bool(outward_consent)),
                approver,
                partition_id,
            ),
        )
        won = cur.rowcount == 1

    outward_sorted = tuple(sorted(assessment.actions))
    if not won:
        current = get_partition(partition_id, tenant=tenant) or {}
        logger.warning(
            "partition.ratify_noop",
            partition_id=partition_id,
            approver=approver,
            status=current.get("status"),
        )
        return RatifyOutcome(
            partition_id=partition_id,
            status=str(current.get("status") or "unknown"),
            ratified_digest=str(current.get("ratified_digest") or ""),
            outward_actions=outward_sorted,
            outward_consent=bool(current.get("outward_consent")),
            diff=str(current.get("ratified_diff") or ""),
            task_ids=tuple(str(r["task_id"]) for r in list_partition_tasks(partition_id)),
            warnings=warnings,
            idempotent=True,
        )

    task_ids = insert_task_rows(partition_id, plan)

    # Audit. `summary` carries the unified diff and is redacted at
    # `record_interaction`'s own choke point; `metadata` deliberately holds
    # ONLY structured, non-secret fields (it is stored verbatim), never the
    # plan blobs -- those already live in `partitions`.
    state_service.record_interaction(
        actor=approver,
        action="partition.ratify",
        target=partition_id,
        summary=diff or "(ratified with no edits to the proposed plan)",
        metadata={
            "partition_id": partition_id,
            "outward_consent": bool(outward_consent),
            "outward_actions": list(outward_sorted),
            "proposed_digest": stored_digest,
            "ratified_digest": ratified_digest,
            "task_count": len(task_ids),
            "edited": bool(diff),
            "warnings": list(warnings),
        },
    )
    logger.info(
        "partition.ratified",
        partition_id=partition_id,
        approver=approver,
        outward_consent=bool(outward_consent),
        outward_actions=list(outward_sorted),
        tasks=len(task_ids),
        edited=bool(diff),
    )
    return RatifyOutcome(
        partition_id=partition_id,
        status="ratified",
        ratified_digest=ratified_digest,
        outward_actions=outward_sorted,
        outward_consent=bool(outward_consent),
        diff=diff,
        task_ids=task_ids,
        warnings=warnings,
    )


def _source_digest_warnings(row: dict[str, Any], plan: PartitionPlan) -> tuple[str, ...]:
    """Warn (never deny) when the ratified plan's `source.digest` no longer
    matches the one recorded at propose time -- the source document drifted
    underneath the plan. v1 warns; a TTL-driven `expired` status is a
    deliberate follow-up (spec section 11.2)."""
    recorded = row.get("source_digest")
    if recorded and plan.source.digest and plan.source.digest != recorded:
        return (
            f"source digest drifted since this partition was proposed "
            f"(proposed against {recorded}, plan now claims {plan.source.digest})",
        )
    return ()


def _record_denial(
    partition_id: str, approver: str, code: str, reason: str, outward_consent: bool
) -> None:
    """Persist a refused ratification attempt.

    A denial is an operator action on a security gate and is worth exactly
    as much audit as an approval -- silently dropping refusals would leave
    "who kept trying to push this through" invisible.
    """
    state_service.record_interaction(
        actor=approver,
        action="partition.ratify_denied",
        target=partition_id,
        summary=reason,
        metadata={
            "partition_id": partition_id,
            "code": code,
            "outward_consent": bool(outward_consent),
        },
    )
    logger.warning(
        "partition.ratify_denied",
        partition_id=partition_id,
        approver=approver,
        code=code,
    )


# ---------------------------------------------------------------------------
# Sprint 3 -- the wave planner (spec section 7)
# ---------------------------------------------------------------------------

#: Task states from which nothing more will ever happen.
TERMINAL_TASK_STATUSES = frozenset({"committed", "failed", "skipped", "cancelled"})

#: How long a `claimed` row with no `run_id` must sit before the startup
#: reconciler rewinds it. A crash leaves such a row behind instantly, but so
#: does a LIVE dispatcher in the microseconds between `claim_task` and
#: `mark_task_running` -- rewinding that one would hand the same task to a
#: second dispatcher, which is the exact double-dispatch this whole
#: claim-before-create design exists to prevent. So the threshold is
#: generous, and it errs towards leaving a row stuck (visible, recoverable by
#: an operator) rather than towards releasing one that is still owned.
STALE_CLAIM_SECONDS = 300


def plan_waves(plan: PartitionPlan) -> tuple[tuple[str, ...], ...]:
    """The partition's `depends_on` DAG as topological LEVELS (spec section 7).

    Wave 0 is every task with no dependencies; wave N+1 is every task all of
    whose dependencies live in waves 0..N. Task ids within a wave are sorted
    for a stable, reproducible plan (the dispatcher's chunking, and every
    test's expectations, depend on the order being deterministic rather than
    dict-insertion-dependent).

    `hivepilot.partition` already rejects cycles, unknown ids and duplicate
    ids at load time, so a cycle cannot normally reach here -- but this
    re-raises `DependencyCycleError` rather than silently returning a partial
    plan if one ever did. A wave planner that quietly drops the tasks it
    could not order would dispatch a SUBSET of a ratified partition, which is
    a different plan from the one the human approved.
    """
    remaining = {task.id: set(task.depends_on) for task in plan.tasks}
    waves: list[tuple[str, ...]] = []
    done: set[str] = set()
    while remaining:
        ready = tuple(sorted(tid for tid, deps in remaining.items() if deps <= done))
        if not ready:
            raise DependencyCycleError(
                "cannot order partition tasks into waves -- unresolvable "
                f"depends_on among {sorted(remaining)}"
            )
        waves.append(ready)
        done.update(ready)
        for tid in ready:
            del remaining[tid]
    return tuple(waves)


# ---------------------------------------------------------------------------
# Effective parallelism (spec section 7) -- surfaced, never assumed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParallelismAssessment:
    """What "N parallel agents" actually means on THIS host's config.

    `runner_throttle` caps the `claude` runner kind at
    `settings.claude_max_concurrency`, whose default is **1** -- so a plan
    asking for `max_parallel: 3` really is one agent, three times, on a
    default install. Surfacing the computed `effective` number (and WHY it is
    what it is, via `notes`) is what keeps the ratify UI from promising
    parallelism the host will not deliver.
    """

    requested: int
    concurrency_limit: int
    runner_cap: int
    runner_kinds: tuple[str, ...]
    effective: int
    notes: tuple[str, ...]


#: Sentinel meaning "this runner kind is not throttled", mirroring
#: `runner_throttle._UNLIMITED`.
_UNLIMITED = 2**31 - 1


def _runner_kinds_for_plan(plan: PartitionPlan) -> tuple[frozenset[str], bool]:
    """The runner kinds this plan's pipelines would actually use.

    Resolution reuses the REAL resolvers (`project_service.load_pipelines`/
    `load_tasks` and `hivepilot.roles.resolve_runner`) rather than
    re-parsing YAML into a second, drifting interpretation of what a stage
    runs.

    Returns `(kinds, fully_resolved)`. `fully_resolved=False` means at least
    one stage could not be resolved, and the caller must then assume the
    THROTTLED kind: an unknown pipeline must never be reported as
    "unthrottled, run them all at once". Under-promising parallelism is
    harmless; over-promising it is the dishonesty this whole assessment
    exists to prevent.
    """
    kinds: set[str] = set()
    fully_resolved = True
    try:
        from hivepilot import roles as roles_module

        pipelines = project_service.load_pipelines().pipelines
        tasks = project_service.load_tasks().tasks
    except Exception:  # noqa: BLE001 - unresolvable config assumes the throttled kind
        return frozenset({_THROTTLED_RUNNER_KIND}), False

    for task in plan.tasks:
        pipeline = pipelines.get(task.pipeline)
        stages = getattr(pipeline, "stages", None) if pipeline is not None else None
        if not stages:
            fully_resolved = False
            continue
        for stage in stages:
            task_def = tasks.get(getattr(stage, "task", "") or "")
            role_name = getattr(task_def, "role", None) if task_def is not None else None
            if not role_name:
                fully_resolved = False
                continue
            try:
                runner_kind, _model, _effort = roles_module.resolve_runner(role_name)
            except Exception:  # noqa: BLE001 - an unresolvable role assumes the throttled kind
                fully_resolved = False
                continue
            kinds.add(str(runner_kind))
    return frozenset(kinds), fully_resolved


#: The one runner kind `hivepilot.services.runner_throttle` actually caps.
_THROTTLED_RUNNER_KIND = "claude"


def effective_parallelism(plan: PartitionPlan) -> ParallelismAssessment:
    """Compute `min(policy.max_parallel, settings.concurrency_limit, runner cap)`.

    Never returns less than 1: a computed cap of zero would mean "dispatch
    nothing", and silently dispatching nothing is a worse failure than
    dispatching serially. A zero/negative configured limit is reported in
    `notes` and floored to 1 rather than honoured.
    """
    notes: list[str] = []
    requested = max(int(plan.policy.max_parallel), 1)

    configured_limit = int(getattr(settings, "concurrency_limit", 1) or 1)
    if configured_limit < 1:
        notes.append(
            f"settings.concurrency_limit={configured_limit} is not a usable cap; treated as 1"
        )
        configured_limit = 1

    kinds, fully_resolved = _runner_kinds_for_plan(plan)
    throttled = _THROTTLED_RUNNER_KIND in kinds or not fully_resolved
    if not fully_resolved:
        notes.append(
            "at least one task's pipeline/role could not be resolved to a runner "
            f"kind; assuming the throttled {_THROTTLED_RUNNER_KIND!r} cap (fail-closed)"
        )
    if throttled:
        runner_cap = max(int(getattr(settings, "claude_max_concurrency", 1) or 1), 1)
        if runner_cap < requested:
            notes.append(
                f"runner_throttle caps {_THROTTLED_RUNNER_KIND!r} at "
                f"claude_max_concurrency={runner_cap}: this plan asks for "
                f"{requested} parallel tasks but will run at most {runner_cap} "
                "at a time"
            )
    else:
        runner_cap = _UNLIMITED

    effective = max(min(requested, configured_limit, runner_cap), 1)
    return ParallelismAssessment(
        requested=requested,
        concurrency_limit=configured_limit,
        runner_cap=runner_cap,
        runner_kinds=tuple(sorted(kinds)),
        effective=effective,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# The dispatcher (spec section 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchOutcome:
    """What one `dispatch_partition` call actually did.

    `halted_reason` is non-`None` whenever the dispatcher stopped before
    every task reached a terminal state -- a paused kill switch, an exhausted
    budget, an unratified partition, or a `halt` failure policy. The
    remaining tasks are left exactly as they were (`pending`) or `cancelled`,
    never silently marked done.
    """

    partition_id: str
    status: str
    waves: tuple[tuple[str, ...], ...]
    effective_parallelism: int
    dispatched: tuple[str, ...] = ()
    committed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    halted_reason: str | None = None


#: A wave's latch wait is bounded by the wave's own declared
#: `wall_clock_seconds` plus this margin (process start-up, git clone, the
#: journal write after the run itself). It is a WAIT bound, not a kill: the
#: dispatcher never terminates a running agent, it only stops waiting for it
#: -- after which the task's dependents see a non-`committed` prerequisite
#: and are honestly `skipped`.
WAVE_WAIT_MARGIN_SECONDS = 120

SubmitFn = Callable[[int, Callable[[], None]], None]


def _default_submit(run_id: int, fn: Callable[[], None]) -> None:
    from hivepilot.services import async_run_service

    async_run_service.submit_run(run_id, fn)


def _resolve_project_name(target: str) -> str:
    """The `runs.project` value for a `<project>` / `<project>/<module>`
    target. Falls back to the raw target when config cannot resolve it, so a
    journal row is still written under a name a human recognizes -- the
    ratification gate already refused unresolvable targets, so this is a
    belt-and-braces path, never the normal one."""
    try:
        project, _module = project_service.resolve_project_target(target)
        return str(project.path.name)
    except Exception:  # noqa: BLE001 - never let a name lookup break a dispatch
        return target


def _create_run_row(*, project_name: str, pipeline: str, tenant: str) -> int:
    """Create the `runs` row for a partition task.

    Deliberately a named module-level indirection rather than an inline call:
    it is the exact instant "claim before create" is about, so it is also the
    exact instant a crash-recovery test needs to interrupt.
    """
    return state_service.record_run_start(project_name, pipeline, status="running", tenant=tenant)


def _run_cost_usd(run_id: int) -> float | None:
    """The measured cost of *run_id*, summed from `steps.cost_usd`.

    Returns `None` -- not `0.0` -- when no step reported a cost: an unpriced
    runner (`shell`, `ansible`, `kubectl`) genuinely has no known cost, and
    recording that as "$0.00 spent" would be a fabricated measurement. The
    journal shows an unknown cost as unknown.
    """
    try:
        steps = state_service.get_steps_for_run(run_id)
    except Exception:  # noqa: BLE001 - cost bookkeeping must never fail a dispatch
        return None
    values = [
        float(step["cost_usd"])
        for step in steps
        if isinstance(step, dict) and step.get("cost_usd") is not None
    ]
    return sum(values) if values else None


def _capture_pr_url(mark: int, project_name: str) -> str | None:
    """The PR URL this task's run opened, or `None`.

    Reads the `git_service` PR ledger for entries recorded after *mark* for
    *project_name*. **Exactly one match is attributed; zero or several are
    `None`.** Two concurrent tasks targeting the same project could each open
    a PR inside the other's window, and there is no honest way to tell them
    apart from here -- so both record `NULL` and the journal shows "—". A
    missing link is a gap; a wrong link is a lie.
    """
    try:
        from hivepilot.services import git_service

        urls = git_service.pr_urls_since(mark, project=project_name)
    except Exception:  # noqa: BLE001 - never let link capture fail an otherwise-good run
        return None
    if len(urls) == 1:
        return urls[0]
    if len(urls) > 1:
        logger.warning(
            "partition.pr_url_ambiguous",
            project=project_name,
            candidates=len(urls),
        )
    return None


def _results_succeeded(results: Any) -> bool:
    """Did `run_pipeline` report success for EVERY result it returned?

    Fail-closed on emptiness: an empty result list is not evidence that the
    work succeeded, it is the absence of evidence, and `committed` is a claim
    the journal makes to a human. A result object without a `success`
    attribute is likewise treated as a failure rather than assumed good.
    """
    try:
        items = list(results)
    except TypeError:
        return False
    if not items:
        return False
    return all(bool(getattr(item, "success", False)) for item in items)


def _wave_halt_reason(
    plan: PartitionPlan, wave_task_ids: Sequence[str], *, tenant: str
) -> str | None:
    """Re-check the kill switch and the budget BEFORE a wave (spec section 7).

    Returns a reason string to halt on, or `None` to proceed. Every
    unresolvable input halts: an unknown daily budget, an unconfigured
    ceiling, or a spend lookup that raises are all "I cannot tell whether
    this is affordable", which must never mean "spend it".
    """
    if autopilot_queue.is_paused(tenant=tenant):
        return "autopilot is paused/stopped for this tenant"

    by_id = {task.id: task for task in plan.tasks}
    wave_tasks = [by_id[tid] for tid in wave_task_ids if tid in by_id]
    if not wave_tasks:
        return None
    wave_cost = sum(task.budget.cost_usd for task in wave_tasks)

    try:
        spent = autopilot_queue.spent_today_usd(tenant=tenant)
    except Exception as exc:  # noqa: BLE001 - unknown spend halts, never proceeds
        return f"daily spend could not be resolved ({exc.__class__.__name__}: {exc})"

    for target in sorted({task.project for task in wave_tasks}):
        policy = get_autopilot_policy(_policy_project_key(target))
        daily = policy.budget_daily_usd
        if daily is None or daily <= 0:
            return f"project {target!r} has no positive budget_daily_usd configured"
        if spent >= daily:
            return (
                f"daily budget for project {target!r} is exhausted "
                f"(spent ${spent:.2f} of ${daily:.2f})"
            )
        if spent + wave_cost > daily:
            return (
                f"this wave's declared budget ${wave_cost:.2f} would exceed the "
                f"remaining daily budget for project {target!r} "
                f"(${daily:.2f} - ${spent:.2f} spent)"
            )
    return None


def _cancel_remaining(partition_id: str, task_ids: Sequence[str]) -> tuple[str, ...]:
    """Cooperatively stop every not-yet-terminal task in *task_ids*.

    A task with a `run_id` gets `async_run_service.request_cancel` -- the
    COOPERATIVE flag the step loop checks at its next boundary. **A running
    agent is never killed**; the process is asked to stop, and if it doesn't,
    it finishes. A task without a run has not started, and
    `mark_task_cancelled` (a `pending|claimed -> cancelled` conditional
    UPDATE) is the whole of its cancellation.
    """
    cancelled: list[str] = []
    rows = {row["task_id"]: row for row in list_partition_tasks(partition_id)}
    for task_id in task_ids:
        row = rows.get(task_id)
        if row is None or str(row.get("status")) in TERMINAL_TASK_STATUSES:
            continue
        run_id = row.get("run_id")
        if run_id is not None:
            try:
                from hivepilot.services import async_run_service

                async_run_service.request_cancel(int(run_id))
            except Exception:  # noqa: BLE001 - best-effort cooperative signal
                logger.warning(
                    "partition.request_cancel_failed",
                    partition_id=partition_id,
                    task_id=task_id,
                )
        if mark_task_cancelled(partition_id, task_id):
            cancelled.append(task_id)
    return tuple(cancelled)


def dispatch_partition(  # noqa: PLR0912, PLR0915 - one linear state machine, kept in one place
    partition_id: str,
    *,
    orchestrator: Any,
    tenant: str = "default",
    claimed_by: str | None = None,
    resume: bool = False,
    submit: SubmitFn | None = None,
) -> DispatchOutcome:
    """Dispatch a RATIFIED partition, wave by wave (spec section 7).

    The gate is `mark_partition_dispatching` -- a conditional
    ``ratified -> dispatching`` UPDATE. It returns `False` from `proposed`,
    which is the persistence-level half of "a partition never dispatches
    without human ratification": there is no code path here that can start a
    task from a proposed plan, because there is no branch that proceeds when
    that transition loses.

    Per task, in this order: **atomic claim -> create the run row ->
    submit**. A crash between the claim and the run row leaves a visible
    `claimed` row with `run_id IS NULL` -- recoverable by
    `reconcile_stale_claims`, and never a double dispatch, because the claim
    itself is the conditional UPDATE that only one caller can win.

    Between waves, the kill switch and the daily budget are re-checked
    (`_wave_halt_reason`), so `hivepilot autopilot pause` halts the next wave
    at the next wave boundary.

    A failed task's dependents are **`skipped`, never `failed`** -- running a
    task whose prerequisite failed is a correctness bug, not a policy choice.
    Independent siblings follow `plan.policy.on_task_failure`
    (`continue` by default); `halt` additionally cooperatively cancels
    everything not yet started.

    *resume* accepts a partition already in `dispatching` (e.g. one halted by
    a pause). This can never double-dispatch: every task still goes through
    `claim_task`, whose ``pending -> claimed`` conditional UPDATE a
    finished/running task can no longer satisfy.
    """
    submit_fn: SubmitFn = submit or _default_submit
    owner = claimed_by or f"dispatcher-{uuid.uuid4().hex[:8]}"

    row = get_partition(partition_id, tenant=tenant)
    if row is None:
        raise PartitionNotFoundError(f"Unknown partition {partition_id!r}.")

    ratified_json = str(row.get("ratified_json") or "")
    if not ratified_json.strip():
        # Fail-closed: no ratified document means no human ever approved a
        # plan, whatever the status column happens to say.
        return DispatchOutcome(
            partition_id=partition_id,
            status=str(row.get("status") or "unknown"),
            waves=(),
            effective_parallelism=0,
            halted_reason="partition has no ratified plan -- nothing may be dispatched",
        )

    try:
        plan = load_partition(ratified_json)
    except PartitionError as exc:
        return DispatchOutcome(
            partition_id=partition_id,
            status=str(row.get("status") or "unknown"),
            waves=(),
            effective_parallelism=0,
            halted_reason=f"ratified plan no longer parses: {exc}",
        )

    waves = plan_waves(plan)
    parallelism = effective_parallelism(plan)

    won = mark_partition_dispatching(partition_id)
    if not won:
        current = str((get_partition(partition_id, tenant=tenant) or {}).get("status") or "unknown")
        if not (resume and current == "dispatching"):
            logger.warning(
                "partition.dispatch_refused",
                partition_id=partition_id,
                status=current,
                resume=resume,
            )
            return DispatchOutcome(
                partition_id=partition_id,
                status=current,
                waves=waves,
                effective_parallelism=parallelism.effective,
                halted_reason=(f"partition is {current!r}, not 'ratified' -- refusing to dispatch"),
            )

    outward_consent = bool(row.get("outward_consent"))
    by_id = {task.id: task for task in plan.tasks}
    dispatched: list[str] = []
    skipped: list[str] = []
    cancelled: list[str] = []
    halted_reason: str | None = None

    logger.info(
        "partition.dispatch_started",
        partition_id=partition_id,
        tenant=tenant,
        waves=len(waves),
        tasks=len(plan.tasks),
        requested_parallelism=parallelism.requested,
        effective_parallelism=parallelism.effective,
        outward_consent=outward_consent,
    )

    for wave_index, wave in enumerate(waves):
        halted_reason = _wave_halt_reason(plan, wave, tenant=tenant)
        if halted_reason:
            logger.info(
                "partition.wave_halted",
                partition_id=partition_id,
                wave=wave_index,
                reason=halted_reason,
            )
            break

        statuses = {
            row["task_id"]: str(row["status"]) for row in list_partition_tasks(partition_id)
        }
        runnable: list[str] = []
        for task_id in wave:
            blockers = [
                dep for dep in by_id[task_id].depends_on if statuses.get(dep) != "committed"
            ]
            if blockers:
                # A prerequisite that did not COMMIT (failed, skipped,
                # cancelled, or still running past its wait bound) means this
                # task must not run. `skipped` -- never `failed`: the task
                # itself did nothing wrong.
                if mark_task_skipped(partition_id, task_id):
                    skipped.append(task_id)
                    logger.info(
                        "partition.task_skipped_unmet_dependency",
                        partition_id=partition_id,
                        task_id=task_id,
                        blockers=blockers,
                    )
                continue
            if statuses.get(task_id) == "pending":
                runnable.append(task_id)

        cap = parallelism.effective
        for chunk_start in range(0, len(runnable), cap):
            chunk = runnable[chunk_start : chunk_start + cap]
            latches = _dispatch_chunk(
                partition_id,
                plan=plan,
                task_ids=chunk,
                owner=owner,
                tenant=tenant,
                outward_consent=outward_consent,
                orchestrator=orchestrator,
                submit_fn=submit_fn,
            )
            dispatched.extend(task_id for task_id, _event, _budget in latches)
            for task_id, event, budget in latches:
                if not event.wait(timeout=budget + WAVE_WAIT_MARGIN_SECONDS):
                    logger.warning(
                        "partition.task_wait_timed_out",
                        partition_id=partition_id,
                        task_id=task_id,
                        waited_seconds=budget + WAVE_WAIT_MARGIN_SECONDS,
                    )

        statuses = {
            row["task_id"]: str(row["status"]) for row in list_partition_tasks(partition_id)
        }
        wave_failed = [tid for tid in wave if statuses.get(tid) == "failed"]
        if wave_failed and plan.policy.on_task_failure == "halt":
            remaining = [tid for w in waves[wave_index + 1 :] for tid in w]
            cancelled.extend(_cancel_remaining(partition_id, remaining))
            halted_reason = (
                f"on_task_failure='halt' and task(s) {sorted(wave_failed)} failed -- "
                "remaining tasks cooperatively cancelled"
            )
            break

    final = {row["task_id"]: str(row["status"]) for row in list_partition_tasks(partition_id)}
    committed = tuple(sorted(tid for tid, st in final.items() if st == "committed"))
    failed = tuple(sorted(tid for tid, st in final.items() if st == "failed"))

    if halted_reason is None and all(st in TERMINAL_TASK_STATUSES for st in final.values()):
        # `dispatching` NEVER auto-completes (spec section 8) -- it is
        # completed here, explicitly, only once every task actually reached a
        # terminal state.
        if failed:
            mark_partition_failed(partition_id)
        else:
            mark_partition_completed(partition_id)

    status = str((get_partition(partition_id, tenant=tenant) or {}).get("status") or "unknown")
    outcome = DispatchOutcome(
        partition_id=partition_id,
        status=status,
        waves=waves,
        effective_parallelism=parallelism.effective,
        dispatched=tuple(dispatched),
        committed=committed,
        failed=failed,
        skipped=tuple(sorted(set(skipped))),
        cancelled=tuple(sorted(set(cancelled))),
        halted_reason=halted_reason,
    )
    logger.info(
        "partition.dispatch_finished",
        partition_id=partition_id,
        status=status,
        dispatched=len(outcome.dispatched),
        committed=len(outcome.committed),
        failed=len(outcome.failed),
        skipped=len(outcome.skipped),
        cancelled=len(outcome.cancelled),
        halted_reason=halted_reason,
    )
    return outcome


def _dispatch_chunk(
    partition_id: str,
    *,
    plan: PartitionPlan,
    task_ids: Sequence[str],
    owner: str,
    tenant: str,
    outward_consent: bool,
    orchestrator: Any,
    submit_fn: SubmitFn,
) -> list[tuple[str, threading.Event, int]]:
    """Claim, create and submit each task in *task_ids*; return their latches.

    The returned `(task_id, done_event, wall_clock_budget)` triples are what
    the caller waits on before opening the next wave -- a wave boundary is a
    real barrier, not a hope.
    """
    by_id = {task.id: task for task in plan.tasks}
    latches: list[tuple[str, threading.Event, int]] = []

    for task_id in task_ids:
        task = by_id[task_id]

        # --- 1. CLAIM (before anything is created) -------------------------
        if not claim_task(partition_id, task_id, claimed_by=owner):
            # Lost the conditional UPDATE: another dispatcher owns this task,
            # or it is no longer `pending`. Never a second dispatch.
            continue

        project_name = _resolve_project_name(task.project)

        # --- 2. CREATE (queue row + run row) -------------------------------
        try:
            queue_id = autopilot_queue.enqueue(
                project_name,
                task.pipeline,
                f"partition {partition_id} task {task_id}",
                tenant=tenant,
                # `running`, not `queued`/`proposed`: this row is dispatched
                # by THIS call, so it must never look dispatchable to anything
                # else. `kind='partition_task'` already keeps `drain_one`
                # away (`next_dispatchable` filters `kind='objective'`); the
                # state is the second, independent reason.
                state="running",
                kind=autopilot_queue.KIND_PARTITION_TASK,
            )
            run_id = _create_run_row(
                project_name=project_name, pipeline=task.pipeline, tenant=tenant
            )
        except Exception:  # noqa: BLE001 - nothing ran, so rewind the claim immediately
            logger.exception(
                "partition.task_create_failed", partition_id=partition_id, task_id=task_id
            )
            release_stale_claim(partition_id, task_id)
            continue

        if not mark_task_running(
            partition_id, task_id, claimed_by=owner, run_id=run_id, queue_id=queue_id
        ):
            # Somebody else moved the row out from under this claim. Do NOT
            # run: the journal, not this function's local state, is the truth.
            # The run/queue rows created a moment ago are resolved rather than
            # left dangling in `running` forever -- a row nothing will ever
            # finish is indistinguishable from a hung run.
            logger.warning(
                "partition.task_running_transition_lost",
                partition_id=partition_id,
                task_id=task_id,
                run_id=run_id,
            )
            try:
                state_service.complete_run(
                    run_id, "failed", "partition task claim was lost before dispatch"
                )
            except Exception:  # noqa: BLE001 - bookkeeping only
                logger.warning("partition.orphan_run_cleanup_failed", run_id=run_id)
            _mark_queue_row(queue_id, "blocked")
            continue

        # --- 3. SUBMIT -----------------------------------------------------
        done = threading.Event()
        submit_fn(
            run_id,
            _make_task_work(
                partition_id,
                task=task,
                owner=owner,
                run_id=run_id,
                queue_id=queue_id,
                project_name=project_name,
                outward_consent=outward_consent,
                orchestrator=orchestrator,
                done=done,
            ),
        )
        latches.append((task_id, done, int(task.budget.wall_clock_seconds)))

    return latches


def _make_task_work(
    partition_id: str,
    *,
    task: Any,
    owner: str,
    run_id: int,
    queue_id: int,
    project_name: str,
    outward_consent: bool,
    orchestrator: Any,
    done: threading.Event,
) -> Callable[[], None]:
    """Build the background callable one partition task runs as.

    `auto_git=outward_consent` reuses the EXISTING, proven runtime
    suppressor (`orchestrator`'s `auto_git` plumbing) rather than adding a
    second one: a partition ratified without outward consent runs with git
    actions suppressed, so nothing is pushed and no PR is opened.
    """

    def _work() -> None:
        from hivepilot.services import git_service

        mark = git_service.pr_ledger_mark()
        try:
            results = orchestrator.run_pipeline(
                project_names=[task.project],
                pipeline_name=task.pipeline,
                extra_prompt=task.prompt,
                auto_git=outward_consent,
                concurrency=1,
            )
        except Exception:  # noqa: BLE001 - never silently swallowed; the journal records it
            logger.exception(
                "partition.task_dispatch_failed",
                partition_id=partition_id,
                task_id=task.id,
                run_id=run_id,
            )
            mark_task_failed(
                partition_id, task.id, claimed_by=owner, cost_usd=_run_cost_usd(run_id)
            )
            _mark_queue_row(queue_id, "blocked")
            done.set()
            return

        cost = _run_cost_usd(run_id)
        if _results_succeeded(results):
            pr_url = _capture_pr_url(mark, project_name) if outward_consent else None
            mark_task_committed(
                partition_id, task.id, claimed_by=owner, pr_url=pr_url, cost_usd=cost
            )
            _mark_queue_row(queue_id, "done", cost_usd=cost)
        else:
            mark_task_failed(partition_id, task.id, claimed_by=owner, cost_usd=cost)
            _mark_queue_row(queue_id, "blocked")
        done.set()

    return _work


def _mark_queue_row(queue_id: int, state: str, *, cost_usd: float | None = None) -> None:
    """Mirror a task's terminal state onto its `autopilot_queue` row so the
    existing queue CLI/panel shows partition work too. Best-effort: the
    journal, not the queue, is the partition's source of truth."""
    try:
        autopilot_queue.mark(queue_id, state, cost_usd=cost_usd)
    except Exception:  # noqa: BLE001 - queue bookkeeping never fails a dispatch
        logger.warning("partition.queue_mark_failed", queue_id=queue_id, state=state)


def dispatch_partition_background(
    partition_id: str,
    *,
    orchestrator: Any,
    tenant: str = "default",
    claimed_by: str | None = None,
    resume: bool = False,
) -> threading.Thread:
    """Run `dispatch_partition` on a daemon thread and return it.

    `dispatch_partition` BLOCKS at every wave boundary (that is what makes a
    wave a barrier), so an HTTP handler must never call it inline. Each
    individual task still goes through `async_run_service.submit_run`, so the
    thread this starts is a coordinator, not a worker.
    """

    def _run() -> None:
        try:
            dispatch_partition(
                partition_id,
                orchestrator=orchestrator,
                tenant=tenant,
                claimed_by=claimed_by,
                resume=resume,
            )
        except Exception:  # noqa: BLE001 - nothing upstream is left to receive this
            logger.exception("partition.background_dispatch_failed", partition_id=partition_id)

    thread = threading.Thread(
        target=_run, name=f"hivepilot-partition-{partition_id[:8]}", daemon=True
    )
    thread.start()
    return thread


def cancel_partition(partition_id: str, *, actor: str, tenant: str = "default") -> tuple[str, ...]:
    """Stop a partition: veto it while `proposed`, else cooperatively cancel
    every task that has not yet reached a terminal state.

    Returns the task ids actually cancelled. A `committed`/`failed` task is
    never rewritten -- history is not editable -- and a RUNNING agent is
    never killed, only asked to stop at its next step boundary.
    """
    row = get_partition(partition_id, tenant=tenant)
    if row is None:
        raise PartitionNotFoundError(f"Unknown partition {partition_id!r}.")

    if str(row.get("status")) == "proposed":
        veto_partition(partition_id, actor=actor)
        return ()

    task_ids = [str(r["task_id"]) for r in list_partition_tasks(partition_id)]
    cancelled = _cancel_remaining(partition_id, task_ids)
    state_service.record_interaction(
        actor=actor,
        action="partition.cancel",
        target=partition_id,
        summary=f"Partition {partition_id} cancelled by {actor}; {len(cancelled)} task(s) stopped.",
        metadata={"partition_id": partition_id, "cancelled_tasks": list(cancelled)},
    )
    logger.info(
        "partition.cancelled", partition_id=partition_id, actor=actor, cancelled=len(cancelled)
    )
    return cancelled


# ---------------------------------------------------------------------------
# The startup reconciler (spec section 8)
# ---------------------------------------------------------------------------


def _claim_is_stale(claimed_at: Any, *, older_than_seconds: int) -> bool:
    """Is a claim old enough to rewind?

    `older_than_seconds <= 0` means "every claim" -- an explicit operator
    instruction, used by the startup sweep of a single-instance deployment.

    Otherwise the timestamp must PARSE and be old enough. An absent or
    unparseable `claimed_at` is treated as NOT stale: rewinding a claim that
    might still be owned hands the same task to a second dispatcher, and a
    double dispatch is strictly worse than a row an operator has to look at.
    """
    if older_than_seconds <= 0:
        return True
    if not isinstance(claimed_at, str) or not claimed_at.strip():
        return False
    text = claimed_at.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return datetime.now(timezone.utc) - parsed >= timedelta(seconds=older_than_seconds)
    return False


def reconcile_stale_claims(
    *, tenant: str | None = None, older_than_seconds: int = STALE_CLAIM_SECONDS
) -> tuple[tuple[str, str], ...]:
    """Rewind crashed claims to `pending`, EXACTLY ONCE (spec section 8).

    Sweeps precisely the rows a crash between "claim" and "create the run
    row" leaves behind: ``status='claimed' AND run_id IS NULL``. A claim that
    DID reach `mark_task_running` has a `run_id` and is therefore never
    rewound -- which is what makes recovery incapable of double-dispatching a
    task that already started.

    "Exactly once" is not a bookkeeping flag: `release_stale_claim` is a
    conditional ``claimed -> pending`` UPDATE, so a second reconciler (or a
    second call) sees `rowcount == 0` and reports nothing. Only rows THIS
    call actually won are returned.

    `dispatching` partitions are deliberately left alone --
    `dispatching` never auto-completes.
    """
    state_service.init_db()
    sql = (
        "SELECT t.partition_id, t.task_id, t.claimed_at FROM partition_tasks t "
        "JOIN partitions p ON p.id = t.partition_id "
        "WHERE t.status='claimed' AND t.run_id IS NULL"
    )
    params: list[Any] = []
    if tenant is not None:
        sql += " AND p.tenant=?"
        params.append(tenant)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()

    released: list[tuple[str, str]] = []
    for row in rows:
        partition_id = str(row["partition_id"])
        task_id = str(row["task_id"])
        if not _claim_is_stale(row["claimed_at"], older_than_seconds=older_than_seconds):
            continue
        if release_stale_claim(partition_id, task_id):
            released.append((partition_id, task_id))

    logger.info(
        "partition.reconciled",
        tenant=tenant,
        candidates=len(rows),
        released=len(released),
        older_than_seconds=older_than_seconds,
    )
    return tuple(released)
