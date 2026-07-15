"""Tests for hivepilot.services.secrets_service.SecretResolver.

Covers the `env` and `file` builtin backends plus the public `resolve()`
entry point (source dispatch, default source, unknown source). Vault and
SOPS have dedicated coverage in tests/test_vault_resolver.py; the registry
mechanism itself has dedicated coverage in tests/test_secrets_registry.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
