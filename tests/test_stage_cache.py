"""Tests for L3 SQLite stage cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services.stage_cache import (
    RedisStageCache,
    SqliteStageCache,
    get_stage_cache,
    stage_cache_key,
)


def test_stage_cache_key_stability() -> None:
    k1 = stage_cache_key("t", "claude-3", "extra", "prior", "abc123")
    k2 = stage_cache_key("t", "claude-3", "extra", "prior", "abc123")
    assert k1 == k2


def test_stage_cache_key_sensitivity() -> None:
    k1 = stage_cache_key("t", "claude-3", "extra", "prior", "abc123")
    k2 = stage_cache_key("t", "claude-3", "extra", "prior", "different-sha")
    assert k1 != k2


def test_stage_cache_key_none_inputs_stable() -> None:
    k1 = stage_cache_key("task", None, None, None, None)
    k2 = stage_cache_key("task", None, None, None, None)
    assert k1 == k2


def test_sqlite_cache_get_put_roundtrip(tmp_path: Path) -> None:
    cache = SqliteStageCache(tmp_path / "test.db")
    assert cache.get("missing-key") is None
    cache.put("k1", "hello world")
    assert cache.get("k1") == "hello world"


def test_sqlite_cache_overwrite(tmp_path: Path) -> None:
    cache = SqliteStageCache(tmp_path / "test.db")
    cache.put("k", "v1")
    cache.put("k", "v2")
    assert cache.get("k") == "v2"


def test_sqlite_cache_creates_parent_dirs(tmp_path: Path) -> None:
    deep_path = tmp_path / "nested" / "deeply" / "cache.db"
    cache = SqliteStageCache(deep_path)
    cache.put("x", "y")
    assert cache.get("x") == "y"


def test_factory_returns_sqlite_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import Settings

    s = Settings(state_db=tmp_path / "s.db", cache_backend="sqlite")
    monkeypatch.setattr(s, "base_dir", tmp_path)
    result = get_stage_cache(s)
    assert isinstance(result, SqliteStageCache)


def test_redis_backend_raises_without_url() -> None:
    with pytest.raises(RuntimeError, match="redis_url"):
        RedisStageCache("")
