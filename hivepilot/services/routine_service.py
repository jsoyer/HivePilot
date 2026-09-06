"""HP-56: per-role routines (crons[], timezone, webhook, replace_key dedup).

YAML ``schedules.yaml`` stays the interval-based daemon config. This module
is the DB-backed, tenant-scoped layer: multiple cron expressions in an IANA
timezone, a persisted ``next_run_at``, an on-demand webhook, and upsert by
``(tenant, replace_key)``. Execution reuses ``Orchestrator.run_task`` plus
the same retry-queue contract as ``schedule_service.run_entry`` — no new
runner kind.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from hivepilot import roles
from hivepilot.services import db, events, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class RoutineError(ValueError):
    """Operator-facing refusal (bad cron, unknown role, colliding key)."""


@dataclass
class Routine:
    id: str
    tenant: str
    role: str
    projects: list[str]
    crons: list[str]
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    enabled: bool
    replace_key: str | None
    created_ts: str | None = None
    updated_ts: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    dt = _aware(value)
    return dt.isoformat() if dt is not None else None


def validate_timezone(name: str) -> str:
    cleaned = (name or "").strip() or "UTC"
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise RoutineError(f"unknown timezone {cleaned!r}") from exc
    return cleaned


def validate_cron(expr: str) -> str:
    cleaned = (expr or "").strip()
    if not cleaned:
        raise RoutineError("cron expression must be non-empty")
    try:
        croniter(cleaned)
    except (ValueError, KeyError, TypeError) as exc:
        raise RoutineError(f"invalid cron {cleaned!r}: {exc}") from exc
    return cleaned


def validate_crons(crons: list[str]) -> list[str]:
    if not crons:
        raise RoutineError("routines need at least one cron expression")
    return [validate_cron(expr) for expr in crons]


def next_fire(crons: list[str], tz_name: str, after: datetime | None = None) -> datetime:
    """Earliest next UTC instant across ``crons`` in ``tz_name`` after ``after``."""
    exprs = validate_crons(crons)
    tz = ZoneInfo(validate_timezone(tz_name))
    base = _aware(after) or _now()
    local_after = base.astimezone(tz)
    soonest: datetime | None = None
    for expr in exprs:
        nxt = croniter(expr, local_after).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=tz)
        utc = nxt.astimezone(timezone.utc)
        if soonest is None or utc < soonest:
            soonest = utc
    assert soonest is not None
    return soonest


def resolve_command_task(role_name: str) -> str:
    try:
        role = roles.get_role(role_name)
    except KeyError as exc:
        raise RoutineError(f"unknown role {role_name!r}") from exc
    task = getattr(role, "command_task", None)
    if not task:
        raise RoutineError(f"role {role_name!r} has no command_task")
    return str(task)


def _normalize_projects(projects: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in projects or []:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out


def _normalize_replace_key(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _row(row: Any) -> Routine:
    mapping = dict(row)
    return Routine(
        id=str(mapping["id"]),
        tenant=str(mapping.get("tenant") or "default"),
        role=str(mapping["role"]),
        projects=list(json.loads(mapping.get("projects") or "[]")),
        crons=list(json.loads(mapping.get("crons") or "[]")),
        timezone=str(mapping.get("timezone") or "UTC"),
        next_run_at=_aware(mapping.get("next_run_at")),
        last_run_at=_aware(mapping.get("last_run_at")),
        enabled=bool(int(mapping.get("enabled") or 0)),
        replace_key=_normalize_replace_key(mapping.get("replace_key")),
        created_ts=str(mapping["created_ts"]) if mapping.get("created_ts") is not None else None,
        updated_ts=str(mapping["updated_ts"]) if mapping.get("updated_ts") is not None else None,
    )


def _fetch(conn: Any, sql: str, params: tuple[Any, ...]) -> list[Routine]:
    rows = conn.execute(db.ph(sql), params).fetchall()
    return [_row(r) for r in rows]


def get_routine(routine_id: str, *, tenant: str | None = "default") -> Routine | None:
    """Lookup by id, or by replace_key when ``routine_id`` matches neither uuid."""
    state_service.init_db()
    with db.connect() as conn:
        found = _fetch(conn, "SELECT * FROM routines WHERE id=?", (routine_id,))
        if not found:
            found = _fetch(
                conn,
                "SELECT * FROM routines WHERE replace_key=?",
                (routine_id,),
            )
        if tenant is not None:
            found = [r for r in found if r.tenant == tenant]
        return found[0] if found else None


def list_routines(
    *,
    tenant: str | None = "default",
    role: str | None = None,
    enabled: bool | None = None,
) -> list[Routine]:
    state_service.init_db()
    clauses = ["1=1"]
    params: list[Any] = []
    if tenant is not None:
        clauses.append("tenant=?")
        params.append(tenant)
    if role:
        clauses.append("role=?")
        params.append(role)
    if enabled is not None:
        clauses.append("enabled=?")
        params.append(1 if enabled else 0)
    sql = f"SELECT * FROM routines WHERE {' AND '.join(clauses)} ORDER BY role, id"
    with db.connect() as conn:
        return _fetch(conn, sql, tuple(params))


def due_routines(*, now: datetime | None = None) -> list[Routine]:
    """Enabled routines whose persisted ``next_run_at`` is at or before now."""
    state_service.init_db()
    stamp = _aware(now) or _now()
    with db.connect() as conn:
        rows = _fetch(
            conn,
            "SELECT * FROM routines WHERE enabled=1 AND next_run_at IS NOT NULL",
            (),
        )
    return [r for r in rows if r.next_run_at is not None and r.next_run_at <= stamp]


def upsert_routine(
    *,
    role: str,
    crons: list[str],
    timezone: str = "UTC",
    projects: list[str] | None = None,
    enabled: bool = True,
    replace_key: str | None = None,
    tenant: str = "default",
    routine_id: str | None = None,
) -> Routine:
    """Create or replace. Same ``(tenant, replace_key)`` keeps the existing id."""
    role_name = (role or "").strip()
    if not role_name:
        raise RoutineError("role is required")
    resolve_command_task(role_name)
    exprs = validate_crons(crons)
    tz_name = validate_timezone(timezone)
    project_names = _normalize_projects(projects)
    key = _normalize_replace_key(replace_key)
    tenant_name = (tenant or "default").strip() or "default"
    nxt = next_fire(exprs, tz_name)

    state_service.init_db()
    existing: Routine | None = None
    if routine_id:
        existing = get_routine(routine_id, tenant=tenant_name)
        if existing is None:
            raise RoutineError(f"unknown routine {routine_id!r}")
    elif key:
        existing = get_routine(key, tenant=tenant_name)

    if key:
        rival = get_routine(key, tenant=tenant_name)
        if rival is not None and (existing is None or rival.id != existing.id):
            raise RoutineError(f"replace_key {key!r} already belongs to routine {rival.id}")

    rid = existing.id if existing is not None else uuid.uuid4().hex
    last_run = existing.last_run_at if existing is not None else None
    created = existing.created_ts if existing is not None else _iso(_now())
    if last_run is not None:
        nxt = next_fire(exprs, tz_name, after=last_run)
    stamp = _iso(_now())
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO routines (
                    id, tenant, role, projects, crons, timezone,
                    next_run_at, last_run_at, enabled, replace_key,
                    created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    role=excluded.role,
                    projects=excluded.projects,
                    crons=excluded.crons,
                    timezone=excluded.timezone,
                    next_run_at=excluded.next_run_at,
                    enabled=excluded.enabled,
                    replace_key=excluded.replace_key,
                    updated_ts=excluded.updated_ts
                """
            ),
            (
                rid,
                tenant_name,
                role_name,
                json.dumps(project_names),
                json.dumps(exprs),
                tz_name,
                _iso(nxt),
                _iso(last_run),
                1 if enabled else 0,
                key,
                created,
                stamp,
            ),
        )
    out = get_routine(rid, tenant=tenant_name)
    assert out is not None
    events.emit(
        "routine.upserted",
        "routine",
        rid,
        tenant=tenant_name,
        payload={"role": role_name, "replace_key": key},
    )
    return out


def patch_routine(
    routine_id: str,
    *,
    tenant: str = "default",
    role: str | None = None,
    crons: list[str] | None = None,
    timezone: str | None = None,
    projects: list[str] | None = None,
    enabled: bool | None = None,
    replace_key: str | None = None,
) -> Routine:
    current = get_routine(routine_id, tenant=tenant)
    if current is None:
        raise RoutineError(f"unknown routine {routine_id!r}")
    return upsert_routine(
        role=role if role is not None else current.role,
        crons=crons if crons is not None else current.crons,
        timezone=timezone if timezone is not None else current.timezone,
        projects=projects if projects is not None else current.projects,
        enabled=current.enabled if enabled is None else enabled,
        replace_key=current.replace_key if replace_key is None else replace_key,
        tenant=current.tenant,
        routine_id=current.id,
    )


def delete_routine(routine_id: str, *, tenant: str = "default") -> bool:
    current = get_routine(routine_id, tenant=tenant)
    if current is None:
        return False
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(db.ph("DELETE FROM routines WHERE id=?"), (current.id,))
    events.emit(
        "routine.deleted",
        "routine",
        current.id,
        tenant=current.tenant,
        payload={"replace_key": current.replace_key},
    )
    return True


def mark_routine_run(routine: Routine, *, when: datetime | None = None) -> Routine:
    """Stamp last_run and persist the next cron fire so the daemon cannot busy-loop."""
    stamp = _aware(when) or _now()
    nxt = next_fire(routine.crons, routine.timezone, after=stamp)
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                UPDATE routines
                SET last_run_at=?, next_run_at=?, updated_ts=?
                WHERE id=?
                """
            ),
            (_iso(stamp), _iso(nxt), _iso(stamp), routine.id),
        )
    updated = get_routine(routine.id, tenant=routine.tenant)
    assert updated is not None
    return updated


def run_routine(
    routine: Routine,
    orchestrator: Any,
    *,
    max_attempts: int = 3,
    base_delay_minutes: int = 2,
    trigger: str = "cron",
) -> bool:
    """Dispatch the role's ``command_task``; always advance cadence afterwards."""
    from hivepilot.services import retry_service

    try:
        task = resolve_command_task(routine.role)
    except RoutineError as exc:
        logger.warning("routine.skipped", routine=routine.id, error=str(exc))
        mark_routine_run(routine)
        return False

    try:
        orchestrator.run_task(
            project_names=list(routine.projects),
            task_name=task,
            extra_prompt=None,
            auto_git=False,
        )
        mark_routine_run(routine)
        events.emit(
            "routine.ran",
            "routine",
            routine.id,
            tenant=routine.tenant,
            payload={"role": routine.role, "task": task, "trigger": trigger},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — same contract as run_entry
        mark_routine_run(routine)
        retry_service.enqueue(
            schedule_name=f"routine:{routine.id}",
            task=task,
            projects=routine.projects,
            error=str(exc),
            attempt=1,
            max_attempts=max_attempts,
            base_delay_minutes=base_delay_minutes,
        )
        events.emit(
            "routine.failed",
            "routine",
            routine.id,
            tenant=routine.tenant,
            payload={"role": routine.role, "task": task, "error": str(exc), "trigger": trigger},
        )
        return False


def to_dict(routine: Routine) -> dict[str, Any]:
    return {
        "id": routine.id,
        "tenant": routine.tenant,
        "role": routine.role,
        "projects": list(routine.projects),
        "crons": list(routine.crons),
        "timezone": routine.timezone,
        "next_run_at": _iso(routine.next_run_at),
        "last_run_at": _iso(routine.last_run_at),
        "enabled": routine.enabled,
        "replace_key": routine.replace_key,
        "created_ts": routine.created_ts,
        "updated_ts": routine.updated_ts,
    }
