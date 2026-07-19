"""
Portable DB abstraction: SQLite (default) or Postgres.

Usage:
    from hivepilot.services import db

    with db.connect() as conn:
        conn.execute(db.ph("SELECT * FROM runs WHERE id = ?"), (run_id,))
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from hivepilot.config import settings

# ── dialect helpers ────────────────────────────────────────────────────────────


def is_postgres() -> bool:
    """True when HIVEPILOT_DATABASE_URL points to a postgres:// or postgresql:// URL."""
    url = settings.database_url
    if not url:
        return False
    return url.startswith(("postgres://", "postgresql://"))


def ph(sql: str) -> str:
    """Translate ? placeholders -> %s for Postgres; no-op for SQLite."""
    if is_postgres():
        return sql.replace("?", "%s")
    return sql


def autoincrement_pk() -> str:
    """Return the correct autoincrement PK fragment for the current dialect."""
    if is_postgres():
        return "BIGSERIAL PRIMARY KEY"
    return "INTEGER PRIMARY KEY AUTOINCREMENT"


# ── column existence check ─────────────────────────────────────────────────────


def column_exists(conn: Any, table: str, col: str) -> bool:
    """
    Portable replacement for PRAGMA table_info guard.

    - SQLite: uses PRAGMA table_info
    - Postgres: uses information_schema.columns
    """
    if is_postgres():
        cur = conn.execute(
            ph("SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?"),
            (table, col),
        )
        return cur.fetchone() is not None
    else:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return any(row["name"] == col for row in cur.fetchall())


# ── insert helper ──────────────────────────────────────────────────────────────


def insert_returning_id(conn: Any, sql: str, params: tuple) -> int:
    """Execute INSERT and return the new row id portably.

    - SQLite: uses cursor.lastrowid
    - Postgres: appends RETURNING id and fetches the result
    """
    if is_postgres():
        sql_pg = ph(sql) + " RETURNING id"
        cur = conn.execute(sql_pg, params)
        return int(cur.fetchone()["id"])
    else:
        cur = conn.execute(sql, params)
        return int(cur.lastrowid)  # type: ignore[arg-type]


# ── SQLite path helper ─────────────────────────────────────────────────────────


def _sqlite_path() -> Path:
    """Resolve the SQLite database file path.

    Reads from state_service.DB_PATH so that test fixtures which monkeypatch
    that attribute are respected (lazy import avoids circular import at module level).
    Always returns a Path even when the attribute is patched to a string.
    """
    # Lazy import to avoid circular: db <- state_service <- db
    from hivepilot.services import state_service  # noqa: PLC0415

    return Path(state_service.DB_PATH)


# ── connection factory ─────────────────────────────────────────────────────────


def _enable_wal_mode(conn: Any) -> None:
    """Switch *conn* to WAL journal mode, retrying on transient contention.

    `PRAGMA journal_mode=WAL` on a brand-new database file requires briefly
    taking an exclusive lock to create the `-wal`/`-shm` side files. When
    several connections race to do this for the very first time against the
    same fresh file (e.g. `init_db()` invoked concurrently from an async-run
    worker thread and the request thread), the loser can get
    `OperationalError: database is locked` -- and, unlike ordinary
    read/write contention, this specific failure is NOT retried by SQLite's
    busy handler (the `timeout=`/`PRAGMA busy_timeout` set on the connection
    below has no effect here), so it surfaces near-instantly instead of
    waiting. A short manual retry loop closes that race: once WAL mode is
    durably set for the file (or another connection wins the race to set it,
    since it's a persistent, file-level property), later attempts see it
    already applied and no longer contend.
    """
    for attempt in range(10):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


@contextmanager
def connect() -> Generator[Any, None, None]:
    """
    Return a context-managed DB connection.

    - SQLite (default): opens _sqlite_path() with WAL, dict-accessible rows
      via sqlite3.Row
    - Postgres: lazy-imports psycopg; raises ImportError with clear message if
      missing; uses dict_row row factory for column-name access
    """
    if is_postgres():
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise ImportError(
                "psycopg is required for Postgres support. "
                "Install it with: pip install psycopg[binary]"
            ) from None

        conn = psycopg.connect(settings.database_url, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        db_path = _sqlite_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # `timeout` (seconds) is sqlite3's busy-timeout: a writer that finds
        # the file locked by another connection waits (retrying internally)
        # up to this long instead of raising `OperationalError: database is
        # locked` immediately. The stdlib default is 5s, which is too tight
        # for concurrent `init_db()` migrations (e.g. the S3 async-run worker
        # thread and the request thread both touching the same sqlite file at
        # startup) under CI's slower/loaded filesystem. 30s gives every
        # writer room to wait its turn without changing schema, query logic,
        # or isolation behaviour -- it only affects how long a blocked writer
        # waits before giving up.
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        _enable_wal_mode(conn)
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
