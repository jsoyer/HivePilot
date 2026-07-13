"""
Portable DB abstraction: SQLite (default) or Postgres.

Usage:
    from hivepilot.services import db

    with db.connect() as conn:
        conn.execute(db.ph("SELECT * FROM runs WHERE id = ?"), (run_id,))
"""

from __future__ import annotations

import sqlite3
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
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
