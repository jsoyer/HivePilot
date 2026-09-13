"""HP-100: unique idempotency_key + volatile skips the effect cache."""

from __future__ import annotations

import pytest

from hivepilot.side_effects import (
    COMPLETED,
    RESERVED,
    SideEffectError,
    cached_result,
    complete,
    get,
    reserve,
)


def test_idempotency_key_is_unique() -> None:
    first = reserve("only-once", token="Read")
    assert first.idempotency_key == "only-once"
    assert first.status == RESERVED
    assert get("only-once") is not None
    with pytest.raises(SideEffectError, match="already exists"):
        reserve("only-once", token="Read")
    with pytest.raises(SideEffectError, match="already exists"):
        reserve("only-once", token="Bash")
    assert get("only-once").token == "Read"


def test_volatile_does_not_cache_effect() -> None:
    reserve("volatile-key", token="WebSearch", volatile=True)
    stored = complete("volatile-key", {"hits": 3})
    assert stored.status == COMPLETED
    assert stored.volatile is True
    assert stored.has_cached_result is False
    assert stored.result is None
    assert cached_result("volatile-key") is None


def test_non_volatile_caches_effect() -> None:
    reserve("stable-key", token="Read", volatile=False)
    stored = complete("stable-key", {"text": "ok"})
    assert stored.has_cached_result is True
    assert stored.result == {"text": "ok"}
    assert cached_result("stable-key") == {"text": "ok"}


def test_empty_key_is_rejected() -> None:
    with pytest.raises(SideEffectError, match="required"):
        reserve("   ", token="Read")
