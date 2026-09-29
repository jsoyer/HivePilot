"""HP-122 — link a pipeline/run verdict to the human decision that consumed it.

``agreement_rows`` joins ``verdicts.pipeline_run_id`` to ``approvals.run_id``.
On a live database that join returns nothing: reviews and human gates do not
share a run, and older verdicts have a NULL ``pipeline_run_id``. A perfect
inferred key cannot measure a gap that is the absence of a shared key.

This module stores the join explicitly. One human decision — approve, reject,
or edit — is keyed by ``run_id`` / ``step`` / ``approval_id`` and points at
the verdict row it consumed. A decision with no verdict on that run is still
stored (``verdict_id`` NULL) so doctor can report it.

Observability only. PASS proposals (HP-97/101) stay in ``pass_proposals``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hivepilot.services import db, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: Stored vocabulary. Callers may pass the approval-row spellings; they are
#: normalised before insert so a query never has to know both.
HITL_DECISIONS: frozenset[str] = frozenset({"approve", "reject", "edit"})

_DECISION_ALIASES: dict[str, str] = {
    "approve": "approve",
    "approved": "approve",
    "reject": "reject",
    "rejected": "reject",
    "deny": "reject",
    "denied": "reject",
    "declined": "reject",
    "edit": "edit",
    "edited": "edit",
}

#: Terminal approval statuses that are a human decision. ``pending`` is not.
_TERMINAL_APPROVAL_STATUSES: frozenset[str] = frozenset({"approved", "denied", "rejected"})

#: ``update_approval(..., "rule")`` is an HP-61 auto-deny, not a person.
_NON_HUMAN_ACTORS: frozenset[str] = frozenset({"rule"})


def normalize_decision(decision: str) -> str:
    """Map an approval status or HITL verb onto approve/reject/edit."""
    token = _DECISION_ALIASES.get((decision or "").strip().lower())
    if token is None:
        raise ValueError(f"decision must be approve, reject, or edit; got {decision!r}")
    return token


def trace_keys_from_approval(approval: dict[str, Any]) -> tuple[str, str]:
    """``(step, approval_id)`` for one approvals row.

    Step checkpoints store ``step_name``; pipeline checkpoints store
    ``next_stage``. A task-level gate has neither, so the task name is the
    step. ``approval_id`` is explicit metadata when a channel set one, and
    the run id otherwise — that is the id Telegram, the CLI, and the API
    already use for this table.
    """
    raw = approval.get("metadata") or "{}"
    meta: dict[str, Any]
    if isinstance(raw, dict):
        meta = raw
    else:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = {}
        meta = loaded if isinstance(loaded, dict) else {}
    step = str(
        meta.get("step_name")
        or meta.get("next_stage")
        or meta.get("task")
        or approval.get("task")
        or ""
    ).strip()
    approval_id = str(meta.get("approval_id") or approval.get("run_id") or "").strip()
    return step, approval_id


def _schema_ready() -> bool:
    """Migrate an existing state DB. Do not create a brand-new empty file.

    ``init_db`` creates ``state.db`` when it is missing. Doctor must not do
    that: a missing file is a different check, and creating one here would
    hide it behind a plausible empty database.
    """
    if db.is_postgres():
        state_service.init_db()
        return True
    path = Path(state_service.DB_PATH)
    if not path.exists():
        return False
    state_service.init_db()
    return True


def resolve_consumed_verdict_id(run_id: int) -> int | None:
    """The verdict this run's human decision consumed, or None.

    Prefer a row stamped with this pipeline run (``pipeline_run_id``). Among
    matches, the latest row wins. A verdict recorded against a different run
    is not consumed: guessing across runs is what made the old join look
    measured when it was empty.
    """
    state_service.init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT id FROM verdicts "
                "WHERE pipeline_run_id = ? OR run_id = ? "
                "ORDER BY CASE WHEN pipeline_run_id = ? THEN 0 ELSE 1 END, id DESC "
                "LIMIT 1"
            ),
            (run_id, run_id, run_id),
        ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def _verdict_exists(verdict_id: int) -> bool:
    state_service.init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT 1 FROM verdicts WHERE id = ?"),
            (verdict_id,),
        ).fetchone()
    return row is not None


def record_verdict_hitl_link(
    *,
    run_id: int,
    step: str,
    approval_id: str,
    decision: str,
    actor: str | None = None,
    verdict_id: int | None = None,
    resolve: bool = True,
) -> dict[str, Any]:
    """Persist one human decision and the verdict it consumed.

    ``verdict_id`` is honoured when that row exists. When it is omitted and
    ``resolve`` is true, the latest verdict on ``run_id`` is consumed. Pass
    ``resolve=False`` to store a snapshot taken before later work records
    new verdicts — including an explicit NULL when the run had none yet.
    A missing verdict is stored as NULL so doctor can report
    « décisions sans verdict ».
    """
    state_service.init_db()
    token = normalize_decision(decision)
    cleaned_step = (step or "").strip()
    cleaned_id = (approval_id or "").strip()
    if not cleaned_step:
        raise ValueError("step is required")
    if not cleaned_id:
        raise ValueError("approval_id is required")

    consumed = verdict_id
    if consumed is None and resolve:
        consumed = resolve_consumed_verdict_id(run_id)
    elif consumed is not None and not _verdict_exists(consumed):
        consumed = None

    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO verdict_hitl_links "
                "(run_id, step, approval_id, verdict_id, decision, actor) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, step, approval_id) DO UPDATE SET "
                "verdict_id = excluded.verdict_id, "
                "decision = excluded.decision, "
                "actor = excluded.actor, "
                "recorded_at = CURRENT_TIMESTAMP"
            ),
            (run_id, cleaned_step, cleaned_id, consumed, token, actor),
        )
    logger.info(
        "verdict_hitl.linked",
        run_id=run_id,
        step=cleaned_step,
        approval_id=cleaned_id,
        verdict_id=consumed,
        decision=token,
    )
    traced = trace_hitl_decision(run_id, cleaned_step, cleaned_id)
    if traced is None:  # pragma: no cover - the row was just written
        raise RuntimeError("verdict hitl link disappeared after insert")
    return traced


def link_approval_decision(
    run_id: int,
    *,
    actor: str | None = None,
    verdict_id: int | None = None,
    resolve: bool = True,
) -> dict[str, Any] | None:
    """Record the join for an approvals row that a human just resolved.

    No-op when the row is still pending, missing, or was written by an
    auto-deny rule. Never raises: bookkeeping must not fail the approval
    the operator already completed.

    ``verdict_id`` + ``resolve=False`` stores the verdict that existed when
    the human decided, not one a resumed pipeline writes afterwards.
    """
    try:
        approval = state_service.get_approval(run_id)
        if not approval:
            return None
        status = (approval.get("status") or "").strip().lower()
        if status not in _TERMINAL_APPROVAL_STATUSES:
            return None
        who = (actor if actor is not None else approval.get("approved_by")) or ""
        if who.strip().lower() in _NON_HUMAN_ACTORS:
            return None
        step, approval_id = trace_keys_from_approval(approval)
        if not step:
            step = "unknown"
        if not approval_id:
            approval_id = str(run_id)
        return record_verdict_hitl_link(
            run_id=run_id,
            step=step,
            approval_id=approval_id,
            decision=status,
            actor=who or None,
            verdict_id=verdict_id,
            resolve=resolve,
        )
    except Exception as exc:  # noqa: BLE001 - observability must not break the gate
        logger.warning("verdict_hitl.link_failed", run_id=run_id, error=str(exc))
        return None


def trace_hitl_decision(run_id: int, step: str, approval_id: str) -> dict[str, Any] | None:
    """The human decision at ``run_id`` / ``step`` / ``approval_id``, plus the verdict it consumed."""
    if not _schema_ready():
        return None
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT l.run_id, l.step, l.approval_id, l.decision, l.actor, l.verdict_id, "
                "v.kind AS verdict_kind, v.decision AS verdict_decision, v.role AS verdict_role, "
                "v.pipeline_run_id AS verdict_pipeline_run_id "
                "FROM verdict_hitl_links l "
                "LEFT JOIN verdicts v ON v.id = l.verdict_id "
                "WHERE l.run_id = ? AND l.step = ? AND l.approval_id = ?"
            ),
            (run_id, step, approval_id),
        ).fetchone()
    if row is None:
        return None
    verdict_id = row["verdict_id"]
    verdict: dict[str, Any] | None = None
    if verdict_id is not None and row["verdict_kind"] is not None:
        verdict = {
            "id": int(verdict_id),
            "kind": row["verdict_kind"],
            "decision": row["verdict_decision"],
            "role": row["verdict_role"],
            "pipeline_run_id": row["verdict_pipeline_run_id"],
        }
    return {
        "run_id": int(row["run_id"]),
        "step": row["step"],
        "approval_id": row["approval_id"],
        "decision": row["decision"],
        "actor": row["actor"],
        "verdict_id": int(verdict_id) if verdict_id is not None else None,
        "verdict": verdict,
    }


def decisions_without_verdict() -> list[dict[str, Any]]:
    """Human decisions that consumed no verdict (« décisions sans verdict »).

    Two populations: a link whose ``verdict_id`` is NULL or dangling, and a
    terminal human approval that was never linked at all. Rule auto-denies
    are not human decisions and are omitted.
    """
    if not _schema_ready():
        return []
    with db.connect() as conn:
        link_rows = conn.execute(
            "SELECT l.run_id, l.step, l.approval_id, l.decision "
            "FROM verdict_hitl_links l "
            "LEFT JOIN verdicts v ON v.id = l.verdict_id "
            "WHERE l.verdict_id IS NULL OR v.id IS NULL "
            "ORDER BY l.run_id, l.step, l.approval_id"
        ).fetchall()
        approval_rows = conn.execute(
            "SELECT run_id, task, metadata, status, approved_by FROM approvals "
            "WHERE status IN ('approved', 'denied', 'rejected') "
            "ORDER BY run_id"
        ).fetchall()
        linked = {
            int(row["run_id"])
            for row in conn.execute("SELECT DISTINCT run_id FROM verdict_hitl_links").fetchall()
        }

    found: list[dict[str, Any]] = [
        {
            "run_id": int(row["run_id"]),
            "step": row["step"],
            "approval_id": row["approval_id"],
            "decision": row["decision"],
            "source": "link",
        }
        for row in link_rows
    ]
    for row in approval_rows:
        run_id = int(row["run_id"])
        if run_id in linked:
            continue
        actor = (row["approved_by"] or "").strip().lower()
        if actor in _NON_HUMAN_ACTORS:
            continue
        step, approval_id = trace_keys_from_approval(dict(row))
        try:
            decision = normalize_decision(row["status"])
        except ValueError:
            decision = row["status"]
        found.append(
            {
                "run_id": run_id,
                "step": step or "unknown",
                "approval_id": approval_id or str(run_id),
                "decision": decision,
                "source": "approval",
            }
        )
    return found


def verdicts_without_decision() -> list[dict[str, Any]]:
    """Verdicts no human decision consumed (« verdicts sans décision »).

    A NULL ``verdicts.decision`` is not a consumable gate outcome (the
    fail-closed review path persists one on purpose) and is excluded.
    """
    if not _schema_ready():
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT v.id, v.run_id, v.pipeline_run_id, v.kind, v.decision, v.role "
            "FROM verdicts v "
            "WHERE v.decision IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM verdict_hitl_links l WHERE l.verdict_id = v.id"
            ") "
            "ORDER BY v.id"
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "run_id": row["run_id"],
            "pipeline_run_id": row["pipeline_run_id"],
            "kind": row["kind"],
            "decision": row["decision"],
            "role": row["role"],
        }
        for row in rows
    ]
