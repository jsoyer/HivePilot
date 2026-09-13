"""HP-100 ``pending_tool`` checkpoint + same-key resume.

Coworker checkpoint pattern, rewritten in Python. Does not vendor TS.

A tool that needs PASS/approval parks a checkpoint with
``kind=pending_tool`` and the reserved ``idempotency_key``. Resume uses
that same key. The effect runs at most once; a crash mid-approval cannot
produce a second execution.

HP-95 ``volatile`` tools skip the effect cache (see ``side_effects``).
This module does not implement memory proposals, presenter parity, skill
doctrine, or trust (HP-101 / HP-102 / HP-103 / HP-105).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from hivepilot.pass_store import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    PassProposal,
    decide,
    get as get_proposal,
    submit,
)
from hivepilot.services import db, state_service
from hivepilot.side_effects import (
    COMPLETED,
    SideEffectError,
    SideEffectRecord,
    bind,
    cached_result,
    complete,
    get as get_effect,
    normalize_key,
    reserve,
)
from hivepilot.tool_catalog import TOOL_APPROVAL_KIND, ToolCatalog, resolve

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
PENDING_TOOL = "pending_tool"
CHECKPOINT_KINDS: tuple[str, ...] = (PENDING_TOOL,)

OPEN = "open"
RESUMING = "resuming"
RESUMED = "resumed"
CHECKPOINT_STATUSES: tuple[str, ...] = (OPEN, RESUMING, RESUMED)

PENDING_APPROVAL = "pending_approval"
REJECTED_STATUS = "rejected"
COMPLETED_STATUS = "completed"

ExecuteFn = Callable[[PassProposal], Any]


class CheckpointError(ValueError):
    """Invalid pending_tool checkpoint or resume."""


@dataclass(frozen=True)
class ToolCheckpoint:
    """One checkpoint row. HP-100 only writes ``kind=pending_tool``."""

    id: str
    kind: str
    idempotency_key: str
    proposal_id: str
    status: str
    payload: dict[str, Any]
    tenant: str = "default"
    created_at: str = ""
    resumed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "idempotency_key": self.idempotency_key,
            "proposal_id": self.proposal_id,
            "status": self.status,
            "payload": dict(self.payload),
            "tenant": self.tenant,
            "created_at": self.created_at,
            "resumed_at": self.resumed_at,
        }


@dataclass(frozen=True)
class ResumeResult:
    """Outcome of ``resume``. ``idempotency_key`` is always the original."""

    idempotency_key: str
    status: str
    executed: bool
    cached: bool
    result: Any = None
    checkpoint: ToolCheckpoint | None = None
    proposal: PassProposal | None = None
    effect: SideEffectRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "executed": self.executed,
            "cached": self.cached,
            "result": self.result,
            "checkpoint": None if self.checkpoint is None else self.checkpoint.to_dict(),
            "proposal_id": "" if self.proposal is None else self.proposal.id,
        }


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                proposal_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                tenant TEXT NOT NULL DEFAULT 'default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resumed_at TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_kind_status "
            "ON checkpoints (kind, status)"
        )


def _parse_payload(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise CheckpointError("payload must be a JSON object")
        return loaded
    raise CheckpointError("payload must be a mapping")


def _row(raw: Any) -> ToolCheckpoint:
    created_at = raw["created_at"]
    resumed_at = raw["resumed_at"]
    return ToolCheckpoint(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        idempotency_key=str(raw["idempotency_key"]),
        proposal_id=str(raw["proposal_id"] or ""),
        status=str(raw["status"]),
        payload=_parse_payload(raw["payload"]),
        tenant=str(raw["tenant"] or "default"),
        created_at="" if created_at is None else str(created_at),
        resumed_at="" if resumed_at is None else str(resumed_at),
    )


def get_checkpoint(idempotency_key: str) -> ToolCheckpoint | None:
    _ensure_table()
    key = normalize_key(idempotency_key)
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM checkpoints WHERE idempotency_key=?"),
            (key,),
        ).fetchone()
    return _row(row) if row else None


def get_checkpoint_by_id(checkpoint_id: str) -> ToolCheckpoint | None:
    _ensure_table()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM checkpoints WHERE id=?"),
            (checkpoint_id,),
        ).fetchone()
    return _row(row) if row else None


def _is_volatile(token: str, catalog: ToolCatalog | None) -> bool:
    decision = resolve(token, catalog=catalog)
    return bool(decision.volatile)


def open_pending_tool(
    *,
    idempotency_key: str,
    token: str,
    payload: Mapping[str, Any] | None = None,
    project: str = "",
    task: str = "",
    change_class: str = "",
    tenant: str = "default",
    catalog: ToolCatalog | None = None,
    policy_overrides: Mapping[str, str] | None = None,
) -> ToolCheckpoint:
    """Persist ``pending_tool`` + PASS **before** any tool effect.

    Re-opening the same key after a crash returns the existing checkpoint
    (same ``idempotency_key``, same proposal). A second key insert is
    refused by ``side_effects``.
    """
    _ensure_table()
    key = normalize_key(idempotency_key)
    token_name = (token or "").strip()
    if not token_name:
        raise CheckpointError("token is required")
    body = _parse_payload(dict(payload) if payload is not None else {})
    body.setdefault("token", token_name)
    volatile = _is_volatile(token_name, catalog)

    existing = get_checkpoint(key)
    if existing is not None:
        if existing.kind != PENDING_TOOL:
            raise CheckpointError(
                f"checkpoint {key} is {existing.kind}, not {PENDING_TOOL}"
            )
        return existing

    effect = get_effect(key)
    if effect is None:
        reserve(key, token=token_name, volatile=volatile)
    elif effect.token and effect.token != token_name:
        raise SideEffectError(f"idempotency_key already exists: {key}")

    proposal = submit(
        kind=TOOL_APPROVAL_KIND,
        project=project,
        task=task,
        action=token_name,
        change_class=change_class,
        payload=body,
        tenant=tenant,
        metadata={"token": token_name, "action": token_name},
        catalog=catalog,
        policy_overrides=policy_overrides,
    )
    row_id = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO checkpoints (
                    id, kind, idempotency_key, proposal_id, status, payload, tenant
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                row_id,
                PENDING_TOOL,
                key,
                proposal.id,
                OPEN,
                json.dumps(body, ensure_ascii=False, sort_keys=True),
                (tenant or "default").strip() or "default",
            ),
        )
    bind(key, proposal_id=proposal.id, checkpoint_id=row_id)
    stored = get_checkpoint(key)
    if stored is None:
        raise CheckpointError(f"failed to persist pending_tool {key}")
    return stored


def _claim_resume(checkpoint_id: str) -> bool:
    """CAS ``open`` → ``resuming``. Exactly one resume wins."""
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                """
                UPDATE checkpoints
                SET status=?
                WHERE id=? AND status=?
                """
            ),
            (RESUMING, checkpoint_id, OPEN),
        )
        return int(cur.rowcount or 0) == 1


def _mark_resumed(checkpoint_id: str) -> None:
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                UPDATE checkpoints
                SET status=?, resumed_at=CURRENT_TIMESTAMP
                WHERE id=?
                """
            ),
            (RESUMED, checkpoint_id),
        )


def _finished(
    key: str,
    *,
    executed: bool,
    cached: bool,
    result: Any,
    checkpoint: ToolCheckpoint | None,
    proposal: PassProposal | None,
    effect: SideEffectRecord | None,
    status: str,
) -> ResumeResult:
    return ResumeResult(
        idempotency_key=key,
        status=status,
        executed=executed,
        cached=cached,
        result=result,
        checkpoint=checkpoint,
        proposal=proposal,
        effect=effect,
    )


def resume(
    idempotency_key: str,
    *,
    execute: ExecuteFn | None = None,
) -> ResumeResult:
    """Resume the same ``idempotency_key``. Effect runs at most once."""
    _ensure_table()
    key = normalize_key(idempotency_key)
    effect = get_effect(key)
    checkpoint = get_checkpoint(key)
    if effect is None or checkpoint is None:
        raise CheckpointError(f"no pending_tool checkpoint for {key}")
    if checkpoint.kind != PENDING_TOOL:
        raise CheckpointError(
            f"checkpoint {key} is {checkpoint.kind}, not {PENDING_TOOL}"
        )

    proposal = get_proposal(checkpoint.proposal_id)
    if effect.status == COMPLETED:
        cached = cached_result(key)
        return _finished(
            key,
            executed=False,
            cached=cached is not None,
            result=cached,
            checkpoint=checkpoint,
            proposal=proposal,
            effect=effect,
            status=COMPLETED_STATUS,
        )
    if checkpoint.status in {RESUMING, RESUMED}:
        return _finished(
            key,
            executed=False,
            cached=False,
            result=None,
            checkpoint=checkpoint,
            proposal=proposal,
            effect=effect,
            status=checkpoint.status,
        )
    if proposal is None:
        raise CheckpointError(f"PASS proposal missing for {key}")
    if proposal.status == PENDING:
        return _finished(
            key,
            executed=False,
            cached=False,
            result=None,
            checkpoint=checkpoint,
            proposal=proposal,
            effect=effect,
            status=PENDING_APPROVAL,
        )
    if proposal.status in {REJECTED, EXPIRED}:
        return _finished(
            key,
            executed=False,
            cached=False,
            result=None,
            checkpoint=checkpoint,
            proposal=proposal,
            effect=effect,
            status=REJECTED_STATUS,
        )
    if proposal.status != APPROVED:
        # EDITED is terminal on PASS but is not an apply signal (HP-101).
        raise CheckpointError(
            f"proposal {proposal.id} is {proposal.status}, not {APPROVED}"
        )

    claimed = _claim_resume(checkpoint.id)
    latest = get_checkpoint(key)
    if not claimed:
        return _finished(
            key,
            executed=False,
            cached=False,
            result=None,
            checkpoint=latest,
            proposal=proposal,
            effect=effect,
            status=latest.status if latest is not None else RESUMING,
        )

    result = execute(proposal) if execute is not None else None
    stored = complete(key, result)
    _mark_resumed(checkpoint.id)
    cached = stored.has_cached_result
    return _finished(
        key,
        executed=True,
        cached=cached,
        result=stored.result if cached else None,
        checkpoint=get_checkpoint(key),
        proposal=proposal,
        effect=stored,
        status=COMPLETED_STATUS,
    )


def approve_and_resume(
    idempotency_key: str,
    *,
    actor: str = "",
    reason: str = "",
    execute: ExecuteFn | None = None,
) -> ResumeResult:
    """Decide APPROVED (persist first) then resume the same key."""
    checkpoint = get_checkpoint(idempotency_key)
    if checkpoint is None:
        raise CheckpointError(f"no pending_tool checkpoint for {idempotency_key}")
    proposal = get_proposal(checkpoint.proposal_id)
    if proposal is None:
        raise CheckpointError(f"PASS proposal missing for {checkpoint.proposal_id}")
    if proposal.status == PENDING:
        decide(proposal.id, "approve", actor=actor, reason=reason)
    return resume(checkpoint.idempotency_key, execute=execute)
