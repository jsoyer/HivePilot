"""Stage memoization cache — pluggable backend.

Default: SQLite (zero infra, reuses the existing state.db).
Optional: Redis (set cache_backend=redis + redis_url for distributed workers).
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.config import Settings

logger = get_logger(__name__)


@runtime_checkable
class StageCache(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, value: str) -> None: ...


class SqliteStageCache:
    """SQLite-backed stage cache, stored in the existing state DB."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_table()

    def _ensure_table(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stage_cache (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage_cache.init_failed", error=str(exc))

    def get(self, key: str) -> str | None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM stage_cache WHERE key = ?", (key,)
                ).fetchone()
            return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage_cache.get_failed", key=key, error=str(exc))
            return None

    def put(self, key: str, value: str) -> None:
        try:
            now = datetime.now(tz=timezone.utc).isoformat()
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO stage_cache (key, value, created_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage_cache.put_failed", key=key, error=str(exc))


class RedisStageCache:
    """Redis-backed stage cache for distributed-worker setups (opt-in)."""

    def __init__(self, redis_url: str) -> None:
        if not redis_url:
            raise RuntimeError(
                "cache_backend=redis requires HIVEPILOT_REDIS_URL to be set (redis_url)."
            )
        try:
            import redis as redis_lib  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "redis package is not installed. "
                "Install it with: pip install redis"
            ) from exc
        self._client = redis_lib.from_url(redis_url)

    def get(self, key: str) -> str | None:
        try:
            val = self._client.get(key)
            return val.decode() if val is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage_cache.redis_get_failed", key=key, error=str(exc))
            return None

    def put(self, key: str, value: str) -> None:
        try:
            self._client.set(key, value.encode())
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage_cache.redis_put_failed", key=key, error=str(exc))


def stage_cache_key(
    task_name: str,
    model: str | None,
    extra_prompt: str | None,
    prior_context: str | None,
    repo_head: str | None,
) -> str:
    """Stable cache key: SHA-256 of all stage inputs that affect output."""
    h = hashlib.sha256()
    for part in (task_name, model or "", extra_prompt or "", prior_context or "", repo_head or ""):
        h.update(part.encode())
    return h.hexdigest()


def get_stage_cache(settings: "Settings") -> StageCache:
    """Factory: return the configured cache backend."""
    backend = settings.cache_backend
    if backend == "redis":
        return RedisStageCache(settings.redis_url or "")
    # Default: sqlite
    db_path = settings.resolve_path(settings.state_db)
    return SqliteStageCache(db_path)
