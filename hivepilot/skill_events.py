"""HP-104 skill-cycle events + ranking (absence of measurement ≠ zero).

OpenSpace ``store.record_skill_event`` pattern, rewritten in Python. This
module does **not** vendor OpenSpace, talk to OpenSpace cloud, persist
pickle embeddings, or implement HP-105 trust / HP-106 signals / HP-108
skill→tools.

Contracts:

- Six event types: ``selected``, ``invoked``, ``applied``, ``completed``,
  ``fallback``, ``excluded``.
- One row per ``(revision_id, run_id, step, event_type)``. A replay returns
  the existing row.
- Ranking never treats a missing measurement as ``0``. Unmeasured skills
  (no events, or events but ``selected == 0``) have ``rate is None`` and
  are omitted from top/bottom.

Revision ids reuse HP-98 ``skill_catalog.revision_id`` so a content-hash
change is a new revision, not a new logical skill. Workshop
``skill_usage_events`` (HP-79) stays a separate append-only log.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hivepilot.services import db, events, state_service
from hivepilot.skill_catalog import logical_skill_id, revision_id, snapshot_hash
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    "selected",
    "invoked",
    "applied",
    "completed",
    "fallback",
    "excluded",
)

SKILL_EVENT_ENTITY_TYPE = "skill_revision"
DEFAULT_RANK_LIMIT = 5


class SkillEventError(ValueError):
    """Invalid skill-cycle event type or identity."""


@dataclass(frozen=True)
class SkillEvent:
    """One idempotent cycle fact for a skill revision on a run step."""

    event_id: str
    event_type: str
    revision_id: str
    logical_id: str
    skill_name: str
    run_id: str
    step: str
    tenant: str
    payload: dict[str, Any]
    created_ts: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "revision_id": self.revision_id,
            "logical_id": self.logical_id,
            "skill_name": self.skill_name,
            "run_id": self.run_id,
            "step": self.step,
            "tenant": self.tenant,
            "payload": dict(self.payload),
            "created_ts": self.created_ts,
        }


@dataclass(frozen=True)
class SkillStats:
    """Per-skill counts. ``rate`` is None when there is no measurement."""

    skill_name: str
    logical_id: str
    selected: int = 0
    invoked: int = 0
    applied: int = 0
    completed: int = 0
    fallback: int = 0
    excluded: int = 0

    @property
    def measured(self) -> bool:
        return (
            self.selected
            + self.invoked
            + self.applied
            + self.completed
            + self.fallback
            + self.excluded
        ) > 0

    @property
    def rate(self) -> float | None:
        """Completed / selected. Missing selected is not zero."""
        if self.selected <= 0:
            return None
        return self.completed / self.selected

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "logical_id": self.logical_id,
            "selected": self.selected,
            "invoked": self.invoked,
            "applied": self.applied,
            "completed": self.completed,
            "fallback": self.fallback,
            "excluded": self.excluded,
            "measured": self.measured,
            "rate": self.rate,
        }


@dataclass(frozen=True)
class SkillRanking:
    """Top/bottom measured skills. Unmeasured names never appear here."""

    top: tuple[SkillStats, ...]
    bottom: tuple[SkillStats, ...]
    unmeasured: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "top": [row.to_dict() for row in self.top],
            "bottom": [row.to_dict() for row in self.bottom],
            "unmeasured": self.unmeasured,
        }


def revision_for_skill(name: str, files: Mapping[str, str] | None = None) -> tuple[str, str]:
    """HP-98 ``(logical_id, revision_id)`` for a skill snapshot."""
    logical = logical_skill_id(name)
    return logical, revision_id(logical, snapshot_hash(files or {}))


def success_rate(selected: int, completed: int) -> float | None:
    """Absence of ``selected`` is not a zero rate."""
    if selected <= 0:
        return None
    return completed / selected


def _event_type(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in EVENT_TYPES:
        raise SkillEventError(
            f"unknown skill event type {value!r}; must be one of {list(EVENT_TYPES)}"
        )
    return cleaned


def _decode_payload(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _row_to_event(row: Mapping[str, Any]) -> SkillEvent:
    data = dict(row)
    return SkillEvent(
        event_id=str(data["id"]),
        event_type=str(data["event_type"]),
        revision_id=str(data["revision_id"]),
        logical_id=str(data["logical_id"]),
        skill_name=str(data["skill_name"]),
        run_id=str(data["run_id"]),
        step=str(data["step"]),
        tenant=str(data["tenant"]),
        payload=_decode_payload(data.get("payload")),
        created_ts=str(data["created_ts"]) if data.get("created_ts") is not None else None,
    )


def record_skill_event(
    *,
    event_type: str,
    revision_id: str,
    run_id: int | str,
    step: str,
    skill_name: str = "",
    logical_id: str = "",
    tenant: str = "default",
    payload: Mapping[str, Any] | None = None,
) -> SkillEvent:
    """Insert one cycle event, or return the existing row for the same key.

    The unique key is ``(revision_id, run_id, step, event_type)``. A second
    call with the same key is a no-op (same ``event_id``).
    """
    kind = _event_type(event_type)
    rev = (revision_id or "").strip()
    run = str(run_id).strip()
    step_name = (step or "").strip()
    if not rev:
        raise SkillEventError("revision_id is required")
    if not run:
        raise SkillEventError("run_id is required")
    if not step_name:
        raise SkillEventError("step is required")
    name = (skill_name or "").strip()
    logical = (logical_id or "").strip() or (logical_skill_id(name) if name else "")
    if not logical:
        raise SkillEventError("logical_id or skill_name is required")
    body = dict(payload or {})
    state_service.init_db()
    event_id = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_cycle_events
                    (id, event_type, revision_id, logical_id, skill_name,
                     run_id, step, tenant, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_id, run_id, step, event_type) DO NOTHING
                """
            ),
            (
                event_id,
                kind,
                rev,
                logical,
                name,
                run,
                step_name,
                tenant,
                json.dumps(body, sort_keys=True, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            db.ph(
                """
                SELECT * FROM skill_cycle_events
                WHERE revision_id = ? AND run_id = ? AND step = ? AND event_type = ?
                """
            ),
            (rev, run, step_name, kind),
        ).fetchone()
    if row is None:
        raise SkillEventError("skill cycle event was not persisted")
    stored = _row_to_event(row)
    if stored.event_id == event_id:
        events.emit(
            f"skill.{kind}",
            SKILL_EVENT_ENTITY_TYPE,
            rev,
            tenant=tenant,
            payload={
                "skill_name": name,
                "logical_id": logical,
                "run_id": run,
                "step": step_name,
                "event_type": kind,
            },
        )
    return stored


def record_cycle(
    items: Sequence[Mapping[str, Any] | str],
    *,
    event_type: str,
    run_id: int | str | None,
    step: str,
    tenant: str = "default",
    extra: Mapping[str, Any] | None = None,
) -> list[SkillEvent]:
    """Record one event type for each skill spec or name.

    Skips when ``run_id`` is missing so dry-run / wiring tests stay silent.
    """
    if run_id is None or str(run_id).strip() == "":
        return []
    recorded: list[SkillEvent] = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
            files: Mapping[str, str] = {}
        else:
            name = str(item.get("name") or "").strip()
            raw_files = item.get("files") or {}
            files = raw_files if isinstance(raw_files, Mapping) else {}
        if not name:
            continue
        logical, rev = revision_for_skill(name, files)
        recorded.append(
            record_skill_event(
                event_type=event_type,
                revision_id=rev,
                logical_id=logical,
                skill_name=name,
                run_id=run_id,
                step=step,
                tenant=tenant,
                payload=extra,
            )
        )
    return recorded


def record_cycle_safe(
    items: Sequence[Mapping[str, Any] | str],
    *,
    event_type: str,
    run_id: int | str | None,
    step: str,
    tenant: str = "default",
    extra: Mapping[str, Any] | None = None,
) -> list[SkillEvent]:
    """Fail-safe wrapper — a telemetry write must never abort a step."""
    try:
        return record_cycle(
            items,
            event_type=event_type,
            run_id=run_id,
            step=step,
            tenant=tenant,
            extra=extra,
        )
    except Exception:  # noqa: BLE001 — usage must never abort a run
        logger.warning("skill_cycle.record_failed", event_type=event_type, exc_info=True)
        return []


def list_skill_events(
    *,
    tenant: str | None = None,
    revision_id: str | None = None,
    run_id: int | str | None = None,
    step: str | None = None,
    event_type: str | None = None,
    skill_name: str | None = None,
    limit: int = 200,
) -> list[SkillEvent]:
    state_service.init_db()
    clauses = ["1=1"]
    args: list[Any] = []
    if tenant is not None:
        clauses.append("tenant = ?")
        args.append(tenant)
    if revision_id:
        clauses.append("revision_id = ?")
        args.append(revision_id)
    if run_id is not None and str(run_id).strip():
        clauses.append("run_id = ?")
        args.append(str(run_id).strip())
    if step:
        clauses.append("step = ?")
        args.append(step)
    if event_type:
        clauses.append("event_type = ?")
        args.append(_event_type(event_type))
    if skill_name:
        clauses.append("skill_name = ?")
        args.append(skill_name)
    args.append(max(1, min(int(limit), 1000)))
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                f"SELECT * FROM skill_cycle_events WHERE {' AND '.join(clauses)} "
                "ORDER BY created_ts ASC LIMIT ?"
            ),
            args,
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def summarize_skills(*, tenant: str = "default") -> list[SkillStats]:
    """Aggregate counts per skill name. Skills with zero rows are absent."""
    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                """
                SELECT skill_name, logical_id, event_type, COUNT(*) AS n
                FROM skill_cycle_events
                WHERE tenant = ?
                GROUP BY skill_name, logical_id, event_type
                """
            ),
            (tenant,),
        ).fetchall()
    buckets: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        name = str(row["skill_name"])
        bucket = buckets.setdefault(
            name,
            {
                "skill_name": name,
                "logical_id": str(row["logical_id"]),
                **{kind: 0 for kind in EVENT_TYPES},
            },
        )
        kind = str(row["event_type"])
        if kind in EVENT_TYPES:
            bucket[kind] = int(row["n"] or 0)
    return [
        SkillStats(
            skill_name=str(bucket["skill_name"]),
            logical_id=str(bucket["logical_id"]),
            selected=int(bucket["selected"]),
            invoked=int(bucket["invoked"]),
            applied=int(bucket["applied"]),
            completed=int(bucket["completed"]),
            fallback=int(bucket["fallback"]),
            excluded=int(bucket["excluded"]),
        )
        for bucket in buckets.values()
    ]


def rank_skills(*, tenant: str = "default", limit: int = DEFAULT_RANK_LIMIT) -> SkillRanking:
    """Top/bottom by measured success rate. Unmeasured skills are omitted."""
    stats = summarize_skills(tenant=tenant)
    measured = [row for row in stats if row.rate is not None]
    unmeasured = sum(1 for row in stats if row.rate is None)
    ordered = sorted(
        measured,
        key=lambda row: (-(row.rate or 0.0), -row.selected, row.skill_name),
    )
    cap = max(1, min(int(limit), 50))
    top = tuple(ordered[:cap])
    bottom = tuple(list(reversed(ordered))[:cap])
    return SkillRanking(top=top, bottom=bottom, unmeasured=unmeasured)
