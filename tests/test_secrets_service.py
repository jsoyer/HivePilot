"""Tests for hivepilot.services.secrets_service.SecretResolver.

Covers the `env` and `file` builtin backends plus the public `resolve()`
entry point (source dispatch, default source, unknown source). Vault and
SOPS have dedicated coverage in tests/test_vault_resolver.py; the registry
mechanism itself has dedicated coverage in tests/test_secrets_registry.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from hivepilot.config import settings
from hivepilot.registry import SECRETS_MAP, SecretRef
from hivepilot.services import secrets_service
from hivepilot.services.config_provenance import (
    clear_secret_values,
    redact_text,
    registered_secret_values,
)
from hivepilot.services.secrets_service import SecretResolver, secret_resolver


def test_module_singleton_is_a_secret_resolver() -> None:
    assert isinstance(secret_resolver, SecretResolver)


def test_from_env_returns_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEPILOT_TEST_SECRET", "sekrit")
    resolver = SecretResolver()
    assert resolver._from_env({"key": "HIVEPILOT_TEST_SECRET"}) == "sekrit"


def test_from_env_raises_runtime_error_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVEPILOT_TEST_SECRET_MISSING", raising=False)
    resolver = SecretResolver()
    with pytest.raises(RuntimeError, match="HIVEPILOT_TEST_SECRET_MISSING"):
        resolver._from_env({"key": "HIVEPILOT_TEST_SECRET_MISSING"})


def test_from_file_reads_and_strips_contents(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("  file-secret-value  \n", encoding="utf-8")
    resolver = SecretResolver()
    assert resolver._from_file({"path": str(secret_file)}) == "file-secret-value"


def test_resolve_defaults_to_env_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEPILOT_DEFAULT_SOURCE_KEY", "default-source-value")
    resolver = SecretResolver()
    result = resolver.resolve({"my_secret": {"key": "HIVEPILOT_DEFAULT_SOURCE_KEY"}})
    assert result == {"my_secret": "default-source-value"}


def test_resolve_multiple_secrets_across_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HIVEPILOT_MULTI_KEY", "env-value")
    secret_file = tmp_path / "multi_secret.txt"
    secret_file.write_text("file-value", encoding="utf-8")

    resolver = SecretResolver()
    result = resolver.resolve(
        {
            "from_env": {"source": "env", "key": "HIVEPILOT_MULTI_KEY"},
            "from_file": {"source": "file", "path": str(secret_file)},
        }
    )
    assert result == {"from_env": "env-value", "from_file": "file-value"}


def test_resolve_unknown_source_raises_value_error() -> None:
    resolver = SecretResolver()
    with pytest.raises(ValueError, match="Unknown secret source"):
        resolver.resolve({"my_secret": {"source": "nope", "key": "x"}})


def test_resolvers_dict_contains_all_builtin_sources() -> None:
    resolver = SecretResolver()
    assert set(resolver.resolvers) == {"env", "file", "vault", "sops"}


# ---------------------------------------------------------------------------
# Secret TTL cache / rotation (Phase 19 follow-up).
#
# Opt-in, in-memory, process-local. `secrets_cache_ttl_seconds == 0` (default)
# = DISABLED = today's always-live behaviour (backend called every resolution,
# resolver does NOT register). When enabled, repeated resolutions within the
# TTL return the cached value without re-hitting the backend, but STILL register
# the value for masking on every hit; after expiry the value is re-fetched.
# ---------------------------------------------------------------------------


class _CountingBackend:
    """A fake secrets backend that returns a fixed value and counts calls."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def resolve(self, ref: SecretRef, _settings: object) -> str:
        self.calls += 1
        return self.value


_CACHE_VALUE = "ttl-cache-SECRET-value-should-not-leak-778899"


@pytest.fixture()
def counting_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CountingBackend]:
    """Register a call-counting backend under source ``counting`` and reset the
    process-local cache + masking registry around the test."""
    backend = _CountingBackend(_CACHE_VALUE)
    SECRETS_MAP["counting"] = backend  # conftest autouse fixture restores SECRETS_MAP
    secrets_service.clear_secret_cache()
    clear_secret_values()
    yield backend
    secrets_service.clear_secret_cache()
    clear_secret_values()


def _spec() -> dict[str, str]:
    return {"source": "counting", "key": "K"}


def test_cache_disabled_by_default_backend_called_every_time(
    counting_backend: _CountingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default TTL == 0 → byte-identical to pre-cache behaviour: no caching path,
    # backend hit every resolution, resolver does NOT register the value itself.
    monkeypatch.setattr(settings, "secrets_cache_ttl_seconds", 0, raising=False)
    resolver = SecretResolver()

    assert resolver.resolve({"s": _spec()}) == {"s": _CACHE_VALUE}
    assert resolver.resolve({"s": _spec()}) == {"s": _CACHE_VALUE}
    assert counting_backend.calls == 2
    # Disabled path never registers on its own (caller does that).
    assert _CACHE_VALUE not in registered_secret_values()


def test_two_resolves_within_ttl_hit_backend_once_and_mask_both(
    counting_backend: _CountingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "secrets_cache_ttl_seconds", 300, raising=False)
    clock = {"t": 1000.0}
    monkeypatch.setattr(secrets_service, "_now", lambda: clock["t"])
    resolver = SecretResolver()

    first = resolver.resolve({"s": _spec()})
    # The first (miss) resolve already registered the value — clear the
    # masking registry so the SECOND (hit) resolve is the ONLY thing that can
    # re-register it. Without this isolation, deleting the hit-branch
    # `register_secret_value` call in `_resolve_cached` would still leave the
    # miss-branch registration in place and this test would pass regardless —
    # it would not actually prove register-on-hit.
    clear_secret_values()
    assert _CACHE_VALUE not in registered_secret_values()

    # A tick later, still inside the TTL window — this resolve MUST be a cache
    # hit (no backend call) yet still register the value for masking.
    clock["t"] = 1010.0
    second = resolver.resolve({"s": _spec()})

    assert first == second == {"s": _CACHE_VALUE}
    assert counting_backend.calls == 1  # cache hit — backend not re-called
    # Masking stays correct on the cache hit too — registered by the HIT path,
    # not a leftover from the earlier miss (which was cleared above).
    assert _CACHE_VALUE in registered_secret_values()
    assert redact_text(f"x {_CACHE_VALUE} y") == "x REDACTED y"


def test_cache_expiry_refetches(
    counting_backend: _CountingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "secrets_cache_ttl_seconds", 60, raising=False)
    clock = {"t": 5000.0}
    monkeypatch.setattr(secrets_service, "_now", lambda: clock["t"])
    resolver = SecretResolver()

    resolver.resolve({"s": _spec()})
    assert counting_backend.calls == 1
    # Advance past the TTL → the cached entry has expired, so it re-fetches.
    clock["t"] = 5000.0 + 61.0
    resolver.resolve({"s": _spec()})
    assert counting_backend.calls == 2


def test_clear_secret_cache_forces_refetch(
    counting_backend: _CountingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "secrets_cache_ttl_seconds", 300, raising=False)
    monkeypatch.setattr(secrets_service, "_now", lambda: 1000.0)
    resolver = SecretResolver()

    resolver.resolve({"s": _spec()})
    assert counting_backend.calls == 1
    secrets_service.clear_secret_cache()
    # Cache emptied → next resolution re-hits the backend even within the TTL.
    resolver.resolve({"s": _spec()})
    assert counting_backend.calls == 2


def test_cache_key_is_spec_sensitive(
    counting_backend: _CountingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different specs must not collide in the cache — each is keyed by a
    stable hash of (source, sorted spec items)."""
    monkeypatch.setattr(settings, "secrets_cache_ttl_seconds", 300, raising=False)
    monkeypatch.setattr(secrets_service, "_now", lambda: 1000.0)
    resolver = SecretResolver()

    resolver.resolve({"a": {"source": "counting", "key": "K1"}})
    resolver.resolve({"b": {"source": "counting", "key": "K2"}})
    # Distinct specs → two backend calls (no false cache hit).
    assert counting_backend.calls == 2
