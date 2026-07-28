"""Guarded autonomous objective queue + fail-closed dispatch gate (Autopilot).

Context: HivePilot already schedules fixed pipelines (`schedule_service.py`)
and already performs gated autonomous action (drift auto-remediation, see
`drift_schedule.py`). This module adds the one missing primitive: a queue a
user-defined "groomer" pipeline can *propose* objectives into, and a gate
that decides whether the engine may dispatch one of those objectives
unattended.

Lifecycle: ``proposed -> queued -> running -> done | blocked | vetoed``.
A human (or a future trusted caller) calls `promote()` to move a proposal
into `queued`; only rows in `queued` (or, if the gate allows it, `proposed`)
are ever candidates for dispatch via `next_dispatchable()`.

FAIL-CLOSED CONTRACT (the security crux of this module)
--------------------------------------------------------
`autopilot_gate()` returns `GateDecision(allow=True, ...)` **only if ALL**
of the following hold; any missing, empty, malformed, or unresolvable input
denies -- this function never allows by default:

  (a) `(project, pipeline)` is present in an EXPLICIT `auto_dispatch`
      allowlist for that project (`AutopilotPolicy.auto_dispatch`) -- a
      missing project entry, an empty list, or an unlisted pipeline all
      deny;
  (b) `AutopilotPolicy.require_approval` is `False` (`True`, or an
      unresolved/`None` policy, denies);
  (c) a positive daily budget is configured (`budget_daily_usd is not None`
      and `> 0`) AND the amount already spent today is strictly less than
      that ceiling -- absent/`None`/non-positive budget denies, as does a
      budget check that raises;
  (d) the resolved pipeline would NOT auto-merge a PR (`git.merge_pr: true`
      on any task the pipeline's stages reference) -- unknown pipelines/
      tasks/config also fail closed to "would auto-merge" (deny).

Disabled-by-default vs. pre-authorized allowlist: a project with no
`auto_dispatch` policy block simply never satisfies (a), so the drain can
*propose* objectives via `enqueue()` but they never advance past `proposed`
without a human `promote()` (and even then, `promote()` alone never
bypasses the gate). A project WITH a non-empty `auto_dispatch` allowlist,
by contrast, is pre-authorizing that (project, pipeline) pair for
unattended dispatch: if every other gate condition also passes, `drain_one()`
dispatches a `proposed` row directly -- no `promote()` required. Rows that
fail any gate condition (unlisted pipeline, exhausted budget, etc.) are the
ones that stay `proposed`/`queued` awaiting a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.outward import OUTWARD_ACTIONS as _OUTWARD_ACTIONS
from hivepilot.services import db, state_service
from hivepilot.services.autopilot_policy import AutopilotPolicy, get_autopilot_policy
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_VALID_STATES = {"proposed", "queued", "running", "done", "blocked", "vetoed"}

# Propose -> ratify -> dispatch PRD (spec §7): the queue TABLE is reused as
# the dispatch substrate for ratified partition tasks, distinguished by an
# additive `kind` column. `drain_one` (the unattended autopilot tick) only
# ever picks `objective` rows, and the partition dispatcher only ever picks
# `partition_task` rows, so the two never contend for the same row.
KIND_OBJECTIVE = "objective"
KIND_PARTITION_TASK = "partition_task"
_VALID_KINDS = {KIND_OBJECTIVE, KIND_PARTITION_TASK}

# Propose -> ratify -> dispatch PRD (spec §6): the CLOSED vocabulary of
# outward-visible actions. "Outward" ("may this become visible outside this
# machine") is a DISTINCT axis from `is_destructive` ("could this damage the
# target system"): `terraform apply` is destructive and not outward;
# `gh pr create` is outward and not destructive. Two independent booleans,
# both of which must be satisfied.
#
# Enforcement now has TWO layers for every token in this set: ADMISSION at
# ratify (`partition_service._check_policy` -- the computed footprint must be
# a subset of the project's `outward_actions` allowlist) and RUNTIME, via the
# run-scoped `hivepilot.outward.OutwardPermission` the dispatcher installs
# around each task's `run_pipeline` call. git/forge additionally keep the
# pre-existing `auto_git` suppressor.
#
# The vocabulary itself is defined ONCE, in `hivepilot.outward`, and
# re-exported here: the admission gate and the runtime gate ask about the
# same object, so they can never drift apart.
OUTWARD_ACTIONS: frozenset[str] = _OUTWARD_ACTIONS


# ---------------------------------------------------------------------------
# Schema (idempotent init, mirrors hivepilot.services.db's connect()/ph()
# idiom -- see that module's docstring)
# ---------------------------------------------------------------------------


def _init_queue_db() -> None:
    with db.connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS autopilot_queue (
                id {db.autoincrement_pk()},
                tenant TEXT NOT NULL DEFAULT 'default',
                project TEXT NOT NULL,
                pipeline TEXT NOT NULL,
                reason TEXT,
                state TEXT NOT NULL DEFAULT 'proposed',
                cost_usd REAL,
                created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Idempotent, additive migration (propose -> ratify -> dispatch PRD,
        # spec §7): every pre-existing row predates partitions and is
        # therefore an `objective` -- which is exactly what the column
        # DEFAULT backfills it to, so `drain_one`'s behaviour on an existing
        # prod DB is byte-identical to before this column existed. Same
        # `_add_column_if_missing` idiom (and the same race-safety) as every
        # `state_service.init_db` migration; imported rather than
        # re-implemented so there is only ever one such helper.
        state_service._add_column_if_missing(
            conn, "autopilot_queue", f"kind TEXT NOT NULL DEFAULT '{KIND_OBJECTIVE}'"
        )


def _init_control_db() -> None:
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopilot_control (
                tenant TEXT PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 0,
                stopped INTEGER NOT NULL DEFAULT 0
            )
            """
        )


@dataclass(frozen=True)
class QueueItem:
    id: int
    tenant: str
    project: str
    pipeline: str
    reason: str | None
    state: str
    cost_usd: float | None
    created_ts: str
    updated_ts: str
    # Additive, defaulted LAST so every existing positional/keyword
    # construction of this dataclass keeps working unchanged.
    kind: str = KIND_OBJECTIVE


def _row_to_item(row: Any) -> QueueItem:
    return QueueItem(
        id=row["id"],
        tenant=row["tenant"],
        project=row["project"],
        pipeline=row["pipeline"],
        reason=row["reason"],
        state=row["state"],
        cost_usd=row["cost_usd"],
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
        kind=_row_kind(row),
    )


def _row_kind(row: Any) -> str:
    """Read a row's `kind`, defaulting to `objective`.

    Fail-safe (not fail-open: `objective` is the MORE restricted bucket for
    the partition dispatcher, and the pre-migration meaning for `drain_one`):
    a row read through a cursor that predates the migration has no `kind`
    key at all, and a NULL from a hand-written INSERT resolves the same way.
    """
    try:
        value = row["kind"]
    except (KeyError, IndexError, TypeError):
        return KIND_OBJECTIVE
    return str(value) if value else KIND_OBJECTIVE


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------


def enqueue(
    project: str,
    pipeline: str,
    reason: str,
    *,
    tenant: str = "default",
    state: str = "proposed",
    kind: str = KIND_OBJECTIVE,
) -> int:
    """Propose a new objective. Never accepts anything but plain strings for
    (project, pipeline, reason) -- callers (e.g. the CLI) must never pass a
    RunResult/detail payload here.

    `kind` defaults to `objective`, so every pre-existing caller enqueues
    exactly the row it did before the column existed. An unknown kind is
    rejected up front (same shape as the `state` guard) rather than silently
    written -- an unrecognized kind would be invisible to BOTH drains.
    """
    if state not in _VALID_STATES:
        raise ValueError(f"Invalid autopilot queue state: {state!r}")
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid autopilot queue kind: {kind!r}")
    _init_queue_db()
    with db.connect() as conn:
        row_id = db.insert_returning_id(
            conn,
            "INSERT INTO autopilot_queue (tenant, project, pipeline, reason, state, kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tenant, project, pipeline, reason, state, kind),
        )
    logger.info(
        "autopilot.enqueue",
        row_id=row_id,
        project=project,
        pipeline=pipeline,
        tenant=tenant,
        state=state,
        kind=kind,
    )
    return row_id


def list_queue(
    *, tenant: str | None = "default", state: str | None = None, kind: str | None = None
) -> list[QueueItem]:
    """List queue rows. `tenant=None` means "all tenants" (never the
    implicit default) -- matches the rest of the codebase's tenant-scoping
    convention. `kind=None` (the default) means "every kind", so existing
    callers see exactly the rows they saw before the column existed."""
    _init_queue_db()
    sql = "SELECT * FROM autopilot_queue WHERE 1=1"
    params: list[Any] = []
    if tenant is not None:
        sql += " AND tenant=?"
        params.append(tenant)
    if state is not None:
        sql += " AND state=?"
        params.append(state)
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY created_ts ASC, id ASC"
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [_row_to_item(r) for r in rows]


def next_dispatchable(*, tenant: str = "default") -> QueueItem | None:
    """Return the oldest `kind='objective'` row in state `queued`; if none,
    the oldest `kind='objective'` row in state `proposed`. This function only
    *picks a candidate* -- it never decides ALLOW/DENY itself; that's
    `autopilot_gate()`'s job.

    The `kind='objective'` filter (propose -> ratify -> dispatch PRD, spec
    §7) is what keeps `drain_one` -- the UNATTENDED autopilot tick -- from
    ever picking up a `partition_task` row. A partition task is dispatched
    only by the partition dispatcher, only after a HUMAN ratified the plan,
    and must never be re-dispatched unattended through `autopilot_gate`
    (whose `require_approval == False` condition is the exact inverse of a
    human-ratified plan). The two drains therefore never contend for the
    same row.
    """
    _init_queue_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT * FROM autopilot_queue WHERE tenant=? AND state='queued' AND kind=? "
                "ORDER BY created_ts ASC, id ASC LIMIT 1"
            ),
            (tenant, KIND_OBJECTIVE),
        ).fetchone()
        if row is None:
            row = conn.execute(
                db.ph(
                    "SELECT * FROM autopilot_queue WHERE tenant=? AND state='proposed' AND kind=? "
                    "ORDER BY created_ts ASC, id ASC LIMIT 1"
                ),
                (tenant, KIND_OBJECTIVE),
            ).fetchone()
    return _row_to_item(row) if row is not None else None


def mark(item_id: int, state: str, *, cost_usd: float | None = None) -> None:
    if state not in _VALID_STATES:
        raise ValueError(f"Invalid autopilot queue state: {state!r}")
    _init_queue_db()
    with db.connect() as conn:
        if cost_usd is not None:
            conn.execute(
                db.ph(
                    "UPDATE autopilot_queue SET state=?, cost_usd=?, "
                    "updated_ts=CURRENT_TIMESTAMP WHERE id=?"
                ),
                (state, cost_usd, item_id),
            )
        else:
            conn.execute(
                db.ph(
                    "UPDATE autopilot_queue SET state=?, updated_ts=CURRENT_TIMESTAMP WHERE id=?"
                ),
                (state, item_id),
            )


def _claim_running(item_id: int) -> bool:
    """Atomically claim *item_id* for dispatch: flips it to `running` only if
    it is still in `queued` or `proposed`. Returns True iff this call won the
    claim (rowcount == 1) -- defense-in-depth against concurrent/re-entrant
    drains double-dispatching the same row (the SELECT in `next_dispatchable`
    and this UPDATE are not otherwise in one transaction)."""
    _init_queue_db()
    with db.connect() as conn:
        cursor = conn.execute(
            db.ph(
                "UPDATE autopilot_queue SET state='running', updated_ts=CURRENT_TIMESTAMP "
                "WHERE id=? AND state IN ('queued', 'proposed')"
            ),
            (item_id,),
        )
        return cursor.rowcount == 1


def promote(item_id: int) -> None:
    """`proposed -> queued` only -- an explicit human (or CLI) action. A
    no-op on rows already past `proposed` (never regresses running/done/
    blocked/vetoed rows back into the dispatch path)."""
    _init_queue_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "UPDATE autopilot_queue SET state='queued', updated_ts=CURRENT_TIMESTAMP "
                "WHERE id=? AND state='proposed'"
            ),
            (item_id,),
        )


def veto(item_id: int) -> None:
    mark(item_id, "vetoed")


# ---------------------------------------------------------------------------
# Kill switch (pause / resume / stop)
# ---------------------------------------------------------------------------


def _get_control(tenant: str) -> tuple[bool, bool]:
    _init_control_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT paused, stopped FROM autopilot_control WHERE tenant=?"), (tenant,)
        ).fetchone()
    if row is None:
        return False, False
    return bool(row["paused"]), bool(row["stopped"])


def _set_control(tenant: str, *, paused: bool | None = None, stopped: bool | None = None) -> None:
    _init_control_db()
    current_paused, current_stopped = _get_control(tenant)
    new_paused = current_paused if paused is None else paused
    new_stopped = current_stopped if stopped is None else stopped
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO autopilot_control (tenant, paused, stopped) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant) DO UPDATE SET paused=excluded.paused, stopped=excluded.stopped"
            ),
            (tenant, int(new_paused), int(new_stopped)),
        )


def is_paused(*, tenant: str = "default") -> bool:
    """True if the drain is paused OR stopped (both halt dispatch)."""
    paused, stopped = _get_control(tenant)
    return paused or stopped


def is_stopped(*, tenant: str = "default") -> bool:
    _, stopped = _get_control(tenant)
    return stopped


def pause(*, tenant: str = "default") -> None:
    _set_control(tenant, paused=True)


def resume(*, tenant: str = "default") -> None:
    _set_control(tenant, paused=False, stopped=False)


def stop(*, tenant: str = "default") -> None:
    _set_control(tenant, stopped=True)


# ---------------------------------------------------------------------------
# Cost ceiling hook
# ---------------------------------------------------------------------------


def spent_today_usd(*, tenant: str = "default") -> float:
    """Injectable cost-ceiling hook.

    Wired to the real Phase-24 analytics cost source
    (`analytics_service.cost_summary`) so the daily budget gate reflects
    actual spend out of the box. Missing/unpriced cost data resolves to
    `0.0` (never fabricated as "over budget", never silently treated as
    unlimited). Kept as a plain module-level function (not a class method)
    so tests/tools can monkeypatch `autopilot_queue.spent_today_usd`
    directly, mirroring this codebase's monkeypatch-the-module-attribute
    test idiom. Callers that need fail-closed behavior on a raised
    exception (e.g. analytics unavailable) must catch it themselves --
    `autopilot_gate`/`drain_one` both do.
    """
    from hivepilot.services import analytics_service  # local import: keep this module import-light

    summary = analytics_service.cost_summary(tenant=tenant, days=1)
    return float(summary.get("overall", {}).get("cost_usd") or 0.0)


# ---------------------------------------------------------------------------
# merge_pr check -- raw YAML only, deliberately NOT importing hivepilot.models
# ---------------------------------------------------------------------------


def _load_raw_yaml(filename: Any) -> dict:
    path = settings.resolve_config_path(filename)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def pipeline_outward_actions(pipeline_name: str) -> frozenset[str]:
    """Which OUTWARD-visible actions (see `OUTWARD_ACTIONS`) would running
    *pipeline_name* perform, according to LIVE config?

    Generalized from `pipeline_would_auto_merge` (now a thin wrapper over
    this function, below) for the propose -> ratify -> dispatch PRD, spec
    §5.3: a ratified partition's computed outward set must be a subset of
    the target project's `outward_actions` allowlist.

    Resolution, per stage of the pipeline:

    | config                                | token          |
    |---------------------------------------|----------------|
    | task `git.push: true`                 | `git_push`     |
    | task `git.create_pr`/`promote_pr`     | `forge_pr`     |
    | task `git.merge_pr: true`             | `forge_merge`  |
    | stage `commits_vault: true`           | `vault_write`  |

    The other declared tokens (`forge_issue`, `forge_release`, `notify`,
    `external_api`) have no static pipeline-config surface to resolve from
    in v1 and are therefore never *derived* here -- but they ARE part of the
    fail-closed full set below, so an unresolvable config still refuses
    them. This is the stated v1 gap from spec §6, not a silent omission.

    **Fail-closed to the FULL set** -- an unknown pipeline, a pipeline with
    no stages, an unknown/unnamed task, or any malformed config all return
    `OUTWARD_ACTIONS` in its entirety, exactly mirroring
    `pipeline_would_auto_merge`'s pre-existing fail-closed-to-`True`
    behaviour (the full set contains `forge_merge`, so the wrapper below
    still answers `True` for every one of those cases). "I cannot tell what
    this pipeline would do" must never resolve to "it would do nothing".

    Deliberately reads `pipelines.yaml`/`tasks.yaml` as raw YAML (not via
    `hivepilot.models.PipelinesFile`/`TasksFile`) so this module keeps its
    zero dependency on `hivepilot/models.py`.

    The whole load-and-walk runs inside a single `try/except Exception`
    backstop: malformed YAML (`yaml.YAMLError`) or a config shape that
    doesn't match expectations (e.g. a `git:`/pipeline/stage entry that is a
    string or list instead of a mapping, raising `AttributeError`/
    `TypeError` from a stray `.get(...)`) must never propagate out of this
    function -- it resolves to the full set, exactly like the explicit
    unknown-pipeline/unknown-task cases below.
    """
    try:
        pipelines = _load_raw_yaml(settings.pipelines_file).get("pipelines") or {}
        tasks = _load_raw_yaml(settings.tasks_file).get("tasks") or {}

        pipeline_def = pipelines.get(pipeline_name)
        if not pipeline_def:
            return OUTWARD_ACTIONS

        stages = pipeline_def.get("stages") or []
        if not stages:
            return OUTWARD_ACTIONS

        actions: set[str] = set()
        for stage in stages:
            task_name = stage.get("task") if isinstance(stage, dict) else None
            if not task_name or task_name not in tasks:
                return OUTWARD_ACTIONS
            if stage.get("commits_vault", False):
                actions.add("vault_write")
            task_def = tasks.get(task_name) or {}
            git_block = task_def.get("git") or {}
            if git_block.get("push", False):
                actions.add("git_push")
            if git_block.get("create_pr", False) or git_block.get("promote_pr", False):
                actions.add("forge_pr")
            if git_block.get("merge_pr", False):
                actions.add("forge_merge")

        return frozenset(actions)
    except Exception as exc:  # noqa: BLE001 - fail-closed: can't tell -> assume everything -> deny
        logger.warning(
            "autopilot.pipeline_outward_actions_malformed_config",
            pipeline=pipeline_name,
            error=f"{exc.__class__.__name__}: {exc}",
        )
        return OUTWARD_ACTIONS


def pipeline_would_auto_merge(pipeline_name: str) -> bool:
    """Would running *pipeline_name* result in an autonomous PR merge
    (`git.merge_pr: true` on any task one of its stages references)?

    A thin wrapper over `pipeline_outward_actions` -- one resolution
    mechanism, not two. Behaviour is unchanged from before that
    generalization: every case that used to return `True` (unknown pipeline,
    no stages, unknown/unnamed task, malformed config, or an actual
    `merge_pr: true`) still does, because all of the fail-closed cases
    resolve to the FULL `OUTWARD_ACTIONS` set, which contains `forge_merge`.

    Kept as a named function (rather than inlining the membership test at
    `autopilot_gate`'s call site) because it is the gate's documented
    condition (d) and both the gate's own tests and the module docstring
    refer to it by name.
    """
    return "forge_merge" in pipeline_outward_actions(pipeline_name)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    allow: bool
    reason: str


def autopilot_gate(
    project: str,
    pipeline: str,
    *,
    policies: AutopilotPolicy | None,
    budget: float | None,
) -> GateDecision:
    """Fail-closed dispatch gate -- see module docstring for the full
    ALLOW/DENY contract.

    `policies` is the caller-resolved `AutopilotPolicy` for *project*
    (typically via `get_autopilot_policy(project)`). `budget` is the amount
    already spent today (typically via `spent_today_usd()`) -- passed in
    rather than resolved internally so this function stays pure and
    trivially testable; callers are responsible for treating a failure to
    resolve either input as a deny (see `drain_one`).
    """
    if not project or not pipeline:
        return GateDecision(False, "missing project or pipeline")

    if policies is None:
        return GateDecision(False, "no autopilot policy resolved")

    allowlist = policies.auto_dispatch or []
    if pipeline not in allowlist:
        return GateDecision(
            False,
            f"pipeline {pipeline!r} not in auto_dispatch allowlist for project {project!r}",
        )

    if policies.require_approval:
        return GateDecision(False, f"require_approval is true for project {project!r}")

    ceiling = policies.budget_daily_usd
    if ceiling is None or ceiling <= 0:
        return GateDecision(False, "no positive budget_daily_usd configured")

    if budget is None or budget >= ceiling:
        return GateDecision(
            False, f"daily budget exhausted or unknown (spent={budget!r} >= ceiling={ceiling!r})"
        )

    if pipeline_would_auto_merge(pipeline):
        return GateDecision(
            False, f"pipeline {pipeline!r} would auto-merge (git.merge_pr: true) -- refusing"
        )

    return GateDecision(True, "allowed")


# ---------------------------------------------------------------------------
# Drain -- at most one objective per tick
# ---------------------------------------------------------------------------


def drain_one(orchestrator: Any, *, tenant: str = "default") -> QueueItem | None:
    """Drain AT MOST ONE queued/proposed objective through the gate this
    tick.

    Returns the `QueueItem` that was inspected (in whatever state it ended
    up in), or `None` if the drain is paused/stopped or nothing is
    dispatchable. On DENY, the row is left exactly as it was (never
    silently advanced) and the reason is logged so Pollen/the CLI can
    surface "awaiting human". On ALLOW, dispatches via
    `orchestrator.run_pipeline(...)` exactly once, then records the cost
    delta this run consumed and marks the row `done`; a raised exception
    from `run_pipeline` marks it `blocked` instead (never silently
    swallowed).
    """
    if is_paused(tenant=tenant):
        logger.info("autopilot.drain_skipped_paused", tenant=tenant)
        return None

    item = next_dispatchable(tenant=tenant)
    if item is None:
        return None

    policy = get_autopilot_policy(item.project)
    try:
        spent_before = spent_today_usd(tenant=tenant)
    except Exception as exc:  # noqa: BLE001 - fail-closed: unknown spend -> deny
        decision = GateDecision(False, f"budget check failed: {exc.__class__.__name__}: {exc}")
    else:
        try:
            decision = autopilot_gate(
                item.project, item.pipeline, policies=policy, budget=spent_before
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed: gate itself must never crash the tick
            decision = GateDecision(False, f"gate check failed: {exc.__class__.__name__}: {exc}")

    if not decision.allow:
        logger.info(
            "autopilot.drain_denied",
            id=item.id,
            project=item.project,
            pipeline=item.pipeline,
            reason=decision.reason,
            tenant=tenant,
        )
        return item

    if not _claim_running(item.id):
        logger.warning(
            "autopilot.drain_claim_lost",
            id=item.id,
            project=item.project,
            pipeline=item.pipeline,
            tenant=tenant,
        )
        return item

    try:
        orchestrator.run_pipeline(
            project_names=[item.project],
            pipeline_name=item.pipeline,
            extra_prompt=None,
            auto_git=False,
        )
    except Exception:  # noqa: BLE001 - never silently swallowed; row reflects the failure
        logger.exception(
            "autopilot.dispatch_failed",
            id=item.id,
            project=item.project,
            pipeline=item.pipeline,
        )
        mark(item.id, "blocked")
        return item

    cost_usd: float | None
    try:
        spent_after = spent_today_usd(tenant=tenant)
        cost_usd = max(spent_after - spent_before, 0.0)
    except Exception:  # noqa: BLE001 - cost bookkeeping failure must not fail the dispatch
        cost_usd = None

    mark(item.id, "done", cost_usd=cost_usd)
    logger.info(
        "autopilot.dispatch_done",
        id=item.id,
        project=item.project,
        pipeline=item.pipeline,
        cost_usd=cost_usd,
    )
    return item
