"""HP-100 idempotency keys + ``side_effects`` persistence.

Coworker tool-gateway pattern, rewritten in Python. Does not vendor TS.

``idempotency_key`` is unique. Resume reuses the same key — it does not
insert a second row. Volatile tools (HP-95 catalog ``volatile: true``)
complete without caching an effect payload.

This module does not execute tools, present approvals, or apply memory /
skill promotions (HP-101 / HP-102 / HP-103 / HP-105).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from hivepilot.services import db, state_service

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
STATUSES: tuple[str, ...] = ("reserved", "completed")
RESERVED = "reserved"
COMPLETED = "completed"


class SideEffectError(ValueError):
    """Invalid idempotency key or unique-key collision."""


@dataclass(frozen=True)
class SideEffectRecord:
    """One ``side_effects`` row keyed by unique ``idempotency_key``."""

    idempotency_key: str
    token: str
    status: str
    volatile: bool
    proposal_id: str = ""
    checkpoint_id: str = ""
    result: Any = None
    created_at: str = ""
    completed_at: str = ""
    _result_raw: str = ""

    @property
    def has_cached_result(self) -> bool:
        return self.status == COMPLETED and not self.volatile and self._result_raw != ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "token": self.token,
            "status": self.status,
            "volatile": self.volatile,
            "proposal_id": self.proposal_id,
            "checkpoint_id": self.checkpoint_id,
            "result": self.result if self.has_cached_result else None,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


def mint_key() -> str:
    return uuid.uuid4().hex


def normalize_key(raw: str) -> str:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise SideEffectError("idempotency_key is required")
    return cleaned


def _is_unique_violation(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    name = type(exc).__name__
    if name in {"IntegrityError", "UniqueViolation"}:
        return True
    text = str(exc).lower()
    return "unique" in text or "duplicate key" in text


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS side_effects (
                idempotency_key TEXT PRIMARY KEY,
                token TEXT NOT NULL DEFAULT '',
                proposal_id TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '',
                volatile INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
            """
        )


def _parse_result(raw: Any) -> Any:
    if raw in (None, ""):
        return None
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _row(raw: Any) -> SideEffectRecord:
    result_raw = "" if raw["result"] is None else str(raw["result"])
    volatile = bool(int(raw["volatile"] or 0))
    status = str(raw["status"])
    created_at = raw["created_at"]
    completed_at = raw["completed_at"]
    cached = status == COMPLETED and not volatile and result_raw != ""
    return SideEffectRecord(
        idempotency_key=str(raw["idempotency_key"]),
        token=str(raw["token"] or ""),
        status=status,
        volatile=volatile,
        proposal_id=str(raw["proposal_id"] or ""),
        checkpoint_id=str(raw["checkpoint_id"] or ""),
        result=_parse_result(result_raw) if cached else None,
        created_at="" if created_at is None else str(created_at),
        completed_at="" if completed_at is None else str(completed_at),
        _result_raw=result_raw,
    )


def get(idempotency_key: str) -> SideEffectRecord | None:
    _ensure_table()
    key = normalize_key(idempotency_key)
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM side_effects WHERE idempotency_key=?"),
            (key,),
        ).fetchone()
    return _row(row) if row else None


def reserve(
    idempotency_key: str,
    *,
    token: str = "",
    volatile: bool = False,
    proposal_id: str = "",
    checkpoint_id: str = "",
) -> SideEffectRecord:
    """Insert a unique ``idempotency_key``. Duplicate keys raise."""
    _ensure_table()
    key = normalize_key(idempotency_key)
    try:
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    """
                    INSERT INTO side_effects (
                        idempotency_key, token, proposal_id, checkpoint_id,
                        status, result, volatile
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    key,
                    (token or "").strip(),
                    (proposal_id or "").strip(),
                    (checkpoint_id or "").strip(),
                    RESERVED,
                    "",
                    1 if volatile else 0,
                ),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise SideEffectError(f"idempotency_key already exists: {key}") from exc
        raise
    stored = get(key)
    if stored is None:
        raise SideEffectError(f"failed to persist idempotency_key {key}")
    return stored


def bind(
    idempotency_key: str,
    *,
    proposal_id: str = "",
    checkpoint_id: str = "",
) -> SideEffectRecord:
    """Attach PASS / checkpoint ids to an existing reservation."""
    _ensure_table()
    key = normalize_key(idempotency_key)
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                UPDATE side_effects
                SET proposal_id=?, checkpoint_id=?
                WHERE idempotency_key=?
                """
            ),
            ((proposal_id or "").strip(), (checkpoint_id or "").strip(), key),
        )
    stored = get(key)
    if stored is None:
        raise SideEffectError(f"idempotency_key not found: {key}")
    return stored


def complete(idempotency_key: str, result: Any = None) -> SideEffectRecord:
    """Mark the key completed. Volatile rows store no effect cache."""
    _ensure_table()
    key = normalize_key(idempotency_key)
    current = get(key)
    if current is None:
        raise SideEffectError(f"idempotency_key not found: {key}")
    payload = "" if current.volatile else json.dumps(result, ensure_ascii=False)
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                UPDATE side_effects
                SET status=?, result=?, completed_at=CURRENT_TIMESTAMP
                WHERE idempotency_key=?
                """
            ),
            (COMPLETED, payload, key),
        )
    stored = get(key)
    if stored is None or stored.status != COMPLETED:
        raise SideEffectError(f"failed to complete idempotency_key {key}")
    return stored


def cached_result(idempotency_key: str) -> Any | None:
    """Return the stored effect, or ``None`` when volatile / incomplete."""
    record = get(idempotency_key)
    if record is None or not record.has_cached_result:
        return None
    return record.result
