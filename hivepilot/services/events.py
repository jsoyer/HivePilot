"""Realtime change/event bus (HP-40, Cycle 1 · P1 "Live & Missions").

The backbone that lets the UI stop polling: every run/step lifecycle write
records a small, ordered fact in a durable `change_log` table, and consumers
either tail that log (SQLite / universal fallback) or get woken instantly by
Postgres `LISTEN/NOTIFY` and then read the same log for the payload. This is
the Agent-Orchestrator pattern — NOTIFY is a wakeup, the log is the source of
truth — so a consumer can reconnect and catch up from its last-seen id with no
missed events, regardless of dialect.

`emit()` is deliberately fail-safe: a broken event write must NEVER break the
run/step write that triggered it, so every failure is logged and swallowed.
The durable log is what makes that safe — a dropped NOTIFY only delays a UI
refresh; the fact is still in `change_log` for the next catch-up read.

The SSE endpoint that turns `subscribe()` into a browser stream is HP-41.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from hivepilot.services import db
from hivepilot.services.db import ph
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: The single Postgres NOTIFY channel + logical `change_log.channel` value.
#: One channel keeps the LISTEN side trivial; `entity_type`/`kind` on each row
#: carry the routing the consumer filters on.
CHANNEL = "hivepilot_events"

#: Postgres NOTIFY payloads are capped (8000 bytes). We only ever send a tiny
#: envelope (the change_log id + kind + entity) and let the consumer read the
#: full row from the log, so we never approach that limit — but keep payloads
#: on `emit()` small for the same reason.
_MAX_PAYLOAD_BYTES = 6000


def emit(
    kind: str,
    entity_type: str,
    entity_id: Any,
    *,
    tenant: str = "default",
    payload: dict[str, Any] | None = None,
    channel: str = CHANNEL,
) -> int | None:
    """Append one change fact to `change_log` and, on Postgres, `pg_notify` a
    small envelope on `channel`. Returns the new `change_log.id`, or `None` if
    anything went wrong (never raises — event emission must not break the
    caller's own write).

    `payload` should stay small (ids + status); consumers read authoritative
    state separately. Anything oversized is dropped from the durable row rather
    than risking a NOTIFY that exceeds Postgres' payload limit.
    """
    try:
        from hivepilot.services import state_service  # lazy: ensure schema exists

        state_service.init_db()
        payload_json = json.dumps(payload) if payload is not None else None
        if payload_json is not None and len(payload_json) > _MAX_PAYLOAD_BYTES:
            payload_json = None  # oversized: keep the fact, drop the body
        with db.connect() as conn:
            change_id = db.insert_returning_id(
                conn,
                "INSERT INTO change_log (channel, kind, entity_type, entity_id, tenant, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (channel, kind, entity_type, str(entity_id), tenant, payload_json),
            )
            if db.is_postgres():
                envelope = json.dumps(
                    {
                        "id": change_id,
                        "kind": kind,
                        "entity_type": entity_type,
                        "entity_id": str(entity_id),
                        "tenant": tenant,
                    }
                )
                conn.execute(ph("SELECT pg_notify(?, ?)"), (channel, envelope))
        return change_id
    except Exception as exc:  # noqa: BLE001 — fail-safe: never break the caller
        logger.warning("events.emit_failed", kind=kind, entity_type=entity_type, error=str(exc))
        return None


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw = out.get("payload")
    if isinstance(raw, str):
        try:
            out["payload"] = json.loads(raw)
        except (ValueError, TypeError):
            out["payload"] = None
    return out


def read_since(
    after_id: int = 0, *, limit: int = 500, channel: str = CHANNEL
) -> list[dict[str, Any]]:
    """Return `change_log` rows with `id > after_id` on `channel`, oldest
    first, capped at `limit`. The catch-up primitive: a reconnecting consumer
    replays from its last-seen id with no gaps and no dupes."""
    from hivepilot.services import state_service  # lazy: ensure schema exists

    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            ph(db.ph("SELECT * FROM change_log WHERE id > ? AND channel = ? ORDER BY id LIMIT ?")),
            (after_id, channel, limit),
        ).fetchall()
    return [_decode(dict(row)) for row in rows]


def latest_change_id(*, channel: str = CHANNEL) -> int:
    """Highest `change_log.id` for `channel` (0 when empty). A fresh subscriber
    starts here so it streams only changes from now on."""
    from hivepilot.services import state_service

    state_service.init_db()
    with db.connect() as conn:
        row = conn.execute(
            ph(db.ph("SELECT COALESCE(MAX(id), 0) AS max_id FROM change_log WHERE channel = ?")),
            (channel,),
        ).fetchone()
    return int(dict(row)["max_id"]) if row else 0


def subscribe(
    after_id: int | None = None,
    *,
    channel: str = CHANNEL,
    poll_interval: float = 0.5,
    stop: Any | None = None,
    idle_timeout: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield change rows as they land, in order, starting after `after_id`
    (defaults to "now" — only future changes).

    Postgres consumers get woken by `LISTEN` and then read the durable log for
    the payload; SQLite (and any dialect, as a fallback) polls the log every
    `poll_interval`s. Either way the log is authoritative, so a missed NOTIFY
    only costs latency, never an event.

    `stop` is an optional object with `is_set()` (e.g. `threading.Event`) to
    end the stream cooperatively; `idle_timeout`, when set, ends the stream
    after that many seconds without a new change (used by the SSE endpoint to
    recycle idle connections). This is a blocking generator — run it on its own
    thread / async executor.
    """
    cursor = latest_change_id(channel=channel) if after_id is None else after_id
    last_activity = time.monotonic()

    if db.is_postgres():
        yield from _subscribe_postgres(
            cursor, channel, poll_interval, stop, idle_timeout, last_activity
        )
        return

    while stop is None or not stop.is_set():
        rows = read_since(cursor, channel=channel)
        if rows:
            for row in rows:
                yield row
                cursor = int(row["id"])
            last_activity = time.monotonic()
        elif idle_timeout is not None and (time.monotonic() - last_activity) >= idle_timeout:
            return
        time.sleep(poll_interval)


def _subscribe_postgres(
    cursor: int,
    channel: str,
    poll_interval: float,
    stop: Any | None,
    idle_timeout: float | None,
    last_activity: float,
) -> Iterator[dict[str, Any]]:
    """Postgres path: `LISTEN` for instant wakeups, falling back to the same
    poll loop if a dedicated listen connection can't be established."""
    try:
        import psycopg
    except ImportError:  # psycopg absent despite a postgres URL — degrade to poll
        yield from _poll_loop(cursor, channel, poll_interval, stop, idle_timeout, last_activity)
        return

    from hivepilot.config import settings

    try:
        conn = psycopg.connect(settings.database_url, autocommit=True)
    except Exception as exc:  # noqa: BLE001 — any connect failure degrades to poll
        logger.warning("events.listen_connect_failed", error=str(exc))
        yield from _poll_loop(cursor, channel, poll_interval, stop, idle_timeout, last_activity)
        return

    try:
        conn.execute(f"LISTEN {channel}")
        # Drain anything already in the log between our start id and now, so a
        # change that landed during connection setup is never missed.
        for row in read_since(cursor, channel=channel):
            yield row
            cursor = int(row["id"])
            last_activity = time.monotonic()
        while stop is None or not stop.is_set():
            got = False
            for _notify in conn.notifies(timeout=poll_interval):
                got = True
            # Whether or not a NOTIFY arrived, the log is the source of truth.
            rows = read_since(cursor, channel=channel)
            for row in rows:
                yield row
                cursor = int(row["id"])
            if rows or got:
                last_activity = time.monotonic()
            elif idle_timeout is not None and (time.monotonic() - last_activity) >= idle_timeout:
                return
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _poll_loop(
    cursor: int,
    channel: str,
    poll_interval: float,
    stop: Any | None,
    idle_timeout: float | None,
    last_activity: float,
) -> Iterator[dict[str, Any]]:
    while stop is None or not stop.is_set():
        rows = read_since(cursor, channel=channel)
        if rows:
            for row in rows:
                yield row
                cursor = int(row["id"])
            last_activity = time.monotonic()
        elif idle_timeout is not None and (time.monotonic() - last_activity) >= idle_timeout:
            return
        time.sleep(poll_interval)
