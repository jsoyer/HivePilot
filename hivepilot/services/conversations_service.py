"""The agents' exchanges, read as conversations rather than as a log.

Every stage's output is already persisted as an ``interactions`` row carrying
the role key in its metadata -- that is how the blocked-gate report finds each
role's reasoning. Nothing ever presented those rows as what they are: one
thread per run, one voice per role.

So this adds no capture. It is a surface on data that has been accumulating all
along, which is why it is a reader and not a recorder.

On replying
-----------
A reply is NOT a message to a running agent: by the time a thread is readable,
its agents have exited. It appends to the role's corrections file -- the path
that already feeds the next run of that role -- and it is attributed to the
**operator**, never to the agent. A corpus that records an operator's
instruction as the agent's own self-correction starts believing its own output.

A chat window that changed nothing would be worse than no chat window, because
it would look like it had.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: How much of one message to hand the browser. Stage outputs run to tens of
#: thousands of characters; the thread view is for reading, and the full text
#: is a click away in the run record. The cut is declared, never silent.
_MAX_BODY_CHARS = 6_000


@dataclass(frozen=True)
class Message:
    """One agent's turn."""

    interaction_id: int
    actor: str
    role: str | None
    action: str
    body: str
    at: str | None


@dataclass(frozen=True)
class Thread:
    run_id: int
    messages: list[Message] = field(default_factory=list)

    @property
    def roles(self) -> list[str]:
        seen: list[str] = []
        for message in self.messages:
            if message.role and message.role not in seen:
                seen.append(message.role)
        return seen


@dataclass(frozen=True)
class RunSummary:
    """One run, as it appears in the list beside the thread."""

    run_id: int
    project: str | None
    started_at: str | None
    message_count: int
    roles: list[str]


def _role_of(raw_metadata: Any) -> str | None:
    """The role key from an interaction's metadata.

    Reads ``metadata["role"]``, never ``actor``: the actor is a tenant-owned
    display label ("Hugo (CISO)") and parsing it back would bake persona naming
    into the engine.
    """
    if not raw_metadata:
        return None
    try:
        meta = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    role = meta.get("role")
    return role.strip().lower() if isinstance(role, str) and role.strip() else None


def _body(text: str | None) -> str:
    """Redacted, bounded, and honest about the cut.

    `record_interaction` already redacts on the way in; this redacts again on
    the way out because the browser is a wider audience than the database, and
    a value registered AFTER a row was written would otherwise still surface.
    """
    from hivepilot.services.config_provenance import redact_text

    body = redact_text((text or "").strip())
    if len(body) <= _MAX_BODY_CHARS:
        return body
    return body[:_MAX_BODY_CHARS] + f"\n\n[truncated: {len(body):,} characters]"


def thread(run_id: int) -> Thread:
    """Every message of *run_id*, oldest first.

    An unknown run is an empty thread, not an error: a Pollen view must not
    fail because a run id went stale in somebody's open tab.
    """
    from hivepilot.services import state_service

    try:
        rows = state_service.list_recent_interactions(limit=500, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - a reader must not break the page
        logger.warning("conversations.thread_failed", run_id=run_id, error=str(exc))
        return Thread(run_id=run_id, messages=[])

    messages = [
        Message(
            interaction_id=int(row.get("id") or 0),
            actor=str(row.get("actor") or "unknown"),
            role=_role_of(row.get("metadata")),
            action=str(row.get("action") or ""),
            body=_body(row.get("summary")),
            at=row.get("timestamp") or row.get("created_at"),
        )
        # `list_recent_interactions` returns newest first; a conversation reads
        # the other way round.
        for row in reversed(rows)
    ]
    return Thread(run_id=run_id, messages=messages)


def recent_runs(limit: int = 25) -> list[RunSummary]:
    """Runs that actually carry messages, newest first.

    Runs with no interactions are excluded: an empty thread in the list is a
    dead end the operator clicks exactly once.
    """
    from hivepilot.services import db, state_service

    state_service.init_db()
    try:
        with db.connect() as conn:
            rows = conn.execute(
                db.ph(
                    "SELECT i.run_id, COUNT(*) AS n, r.project, r.started_at "
                    "FROM interactions i LEFT JOIN runs r ON r.id = i.run_id "
                    "WHERE i.run_id IS NOT NULL "
                    "GROUP BY i.run_id ORDER BY i.run_id DESC LIMIT ?"
                ),
                (int(limit),),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("conversations.list_failed", error=str(exc))
        return []

    summaries: list[RunSummary] = []
    for row in rows:
        run_id = int(row[0])
        summaries.append(
            RunSummary(
                run_id=run_id,
                project=row[2],
                started_at=row[3],
                message_count=int(row[1]),
                roles=thread(run_id).roles,
            )
        )
    return summaries


def _append_correction(role_key: str, text: str, *, commit: bool = True, author: str = "agent"):
    """Indirection so the reply path is testable without a git repository."""
    from hivepilot.services.corrections_service import append_correction

    return append_correction(role_key, text, commit=commit, author=author)


def reply(*, role: str, text: str) -> str:
    """Record an operator instruction for *role*, feeding its next run.

    Fails closed on both arguments. An unknown role would write a corrections
    file nothing ever reads -- select, never invent -- and an empty reply would
    commit a dated no-op into the corpus.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("a reply cannot be empty")

    from hivepilot.roles import get_role

    try:
        get_role(role)
    except Exception as exc:  # noqa: BLE001 - unknown role, refuse rather than create
        raise ValueError(f"unknown role {role!r}") from exc

    path = _append_correction(role, body, commit=True, author="operator")
    logger.info("conversations.replied", role=role, chars=len(body))
    return str(path)
