"""Tests for hivepilot.services.secret_refs — the ${secret:NAME} reference
parser + lazy resolution through the existing secret_resolver, with the
configurable fail mode (closed | fallback).

Masking behaviour (that resolved values are redacted from logs/state) has its
own dedicated file: tests/test_secret_masking.py.
"""

from __future__ import annotations

import logging

import pytest

from hivepilot.services import config_provenance, secret_refs


@pytest.fixture(autouse=True)
def _clear_masks() -> None:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


# --- parser -----------------------------------------------------------------


def test_find_secret_refs_extracts_names() -> None:
    assert secret_refs.find_secret_refs("${secret:openai}") == ["openai"]
    assert secret_refs.find_secret_refs("a-${secret:foo}-b-${secret:bar}") == ["foo", "bar"]


def test_has_secret_ref_true_and_false() -> None:
    assert secret_refs.has_secret_ref("${secret:x}") is True
    assert secret_refs.has_secret_ref("plain") is False


def test_parser_leaves_other_dollar_brace_tokens_untouched() -> None:
    # ${PWD} and ${OTHER} are NOT secret refs and must be ignored.
    assert secret_refs.find_secret_refs("${PWD}:/workspace") == []
    assert secret_refs.has_secret_ref("prefix-${PWD}-suffix") is False
    assert secret_refs.has_secret_ref("${env:FOO}") is False


# --- resolution: closed mode (default) --------------------------------------


def test_resolve_ref_from_env_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_REF_STORE", "resolved-value")
    catalog = {"openai": {"source": "env", "key": "HP_REF_STORE"}}
    out = secret_refs.resolve_secret_refs(
        {"OPENAI_API_KEY": "${secret:openai}"}, catalog=catalog, fail_mode="closed"
    )
    assert out == {"OPENAI_API_KEY": "resolved-value"}


def test_resolve_ref_embedded_in_larger_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_REF_STORE", "abc")
    catalog = {"tok": {"source": "env", "key": "HP_REF_STORE"}}
    out = secret_refs.resolve_secret_refs(
        {"URL": "https://user:${secret:tok}@host"}, catalog=catalog, fail_mode="closed"
    )
    assert out == {"URL": "https://user:abc@host"}


def test_values_without_refs_are_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_REF_STORE", "v")
    catalog = {"tok": {"source": "env", "key": "HP_REF_STORE"}}
    out = secret_refs.resolve_secret_refs(
        {"PLAIN": "no-ref", "REF": "${secret:tok}"}, catalog=catalog, fail_mode="closed"
    )
    assert "PLAIN" not in out
    assert out == {"REF": "v"}


def test_closed_missing_catalog_name_aborts_with_name_only() -> None:
    catalog: dict[str, dict] = {}
    with pytest.raises(secret_refs.SecretReferenceError) as exc:
        secret_refs.resolve_secret_refs(
            {"K": "${secret:absent}"}, catalog=catalog, fail_mode="closed"
        )
    assert "absent" in str(exc.value)


def test_closed_provider_error_aborts_name_and_provider_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HP_MISSING_ENV", raising=False)
    catalog = {"secretname": {"source": "env", "key": "HP_MISSING_ENV"}}
    with pytest.raises(secret_refs.SecretReferenceError) as exc:
        secret_refs.resolve_secret_refs(
            {"K": "${secret:secretname}"}, catalog=catalog, fail_mode="closed"
        )
    msg = str(exc.value)
    # Names the reference NAME and the provider NAME...
    assert "secretname" in msg
    assert "env" in msg
    # ...but never the underlying env key that failed (avoid leaking store keys).
    assert "HP_MISSING_ENV" not in msg


# --- resolution: fallback mode ----------------------------------------------


def test_fallback_uses_env_named_after_ref_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Provider (vault) errors; fallback finds an env var literally named after
    # the reference and succeeds, logging a WARNING that names the provider only.
    monkeypatch.setenv("myref", "fallback-value")
    catalog = {"myref": {"source": "vault", "path": "secret/x", "key": "k"}}
    with caplog.at_level(logging.WARNING):
        out = secret_refs.resolve_secret_refs(
            {"K": "${secret:myref}"}, catalog=catalog, fail_mode="fallback"
        )
    assert out == {"K": "fallback-value"}
    assert any("fallback" in rec.message or "fallback" in str(rec.msg) for rec in caplog.records)


def test_fallback_with_nothing_to_fall_back_to_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("nope", raising=False)
    catalog = {"nope": {"source": "env", "key": "ALSO_MISSING"}}
    monkeypatch.delenv("ALSO_MISSING", raising=False)
    with pytest.raises(secret_refs.SecretReferenceError):
        secret_refs.resolve_secret_refs(
            {"K": "${secret:nope}"}, catalog=catalog, fail_mode="fallback"
        )


# --- registration for masking ----------------------------------------------


def test_resolved_values_are_registered_for_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_REF_STORE", "UNIQUE-MARKER-XYZ")
    catalog = {"tok": {"source": "env", "key": "HP_REF_STORE"}}
    secret_refs.resolve_secret_refs({"K": "${secret:tok}"}, catalog=catalog, fail_mode="closed")
    assert "UNIQUE-MARKER-XYZ" in config_provenance.registered_secret_values()
