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
import uuid
from dataclasses import dataclass
from typing import Any

from hivepilot.partition import PartitionError, PartitionPlan, load_partition
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
