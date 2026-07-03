"""
Tests for hivepilot.services.token_service — expiry, rotation, add_token tuple return.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import hivepilot.services.state_service as state_service
import hivepilot.services.token_service as token_service
from hivepilot.services.token_service import TokenEntry


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect DB_PATH to a temp file for every test."""
    db = tmp_path / "test_tokens.db"
    monkeypatch.setattr(state_service, "DB_PATH", db)
    return db


@pytest.fixture()
def tmp_tokens_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point token_service (and settings) at a temp YAML file."""
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")

    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


class TestAddTokenReturnsTuple:
    """add_token() must return (raw_token, TokenEntry)."""

    def test_add_token_returns_tuple(self, tmp_tokens_file: Path) -> None:
        """add_token returns a 2-tuple (raw_token, entry)."""
        result = token_service.add_token("admin")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_add_token_raw_equals_entry_token(self, tmp_tokens_file: Path) -> None:
        """entry.token is the SHA-256 hash of the returned plaintext (not equal to raw)."""
        import hashlib

        raw, entry = token_service.add_token("admin")
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        assert entry.token == expected_hash
        assert raw != entry.token

    def test_add_token_role_stored(self, tmp_tokens_file: Path) -> None:
        """entry.role matches the requested role."""
        raw, entry = token_service.add_token("run", note="ci-bot")
        assert entry.role == "run"
        assert entry.note == "ci-bot"

    def test_add_token_no_ttl_gives_none_expires_at(self, tmp_tokens_file: Path) -> None:
        """Without ttl_days, expires_at is None."""
        raw, entry = token_service.add_token("read")
        assert entry.expires_at is None

    def test_add_token_ttl_days_sets_expires_at(self, tmp_tokens_file: Path) -> None:
        """ttl_days=30 sets expires_at ~30 days from now."""
        before = datetime.now(timezone.utc)
        raw, entry = token_service.add_token("admin", ttl_days=30)
        after = datetime.now(timezone.utc)

        assert entry.expires_at is not None
        assert entry.expires_at >= before + timedelta(days=29, hours=23)
        assert entry.expires_at <= after + timedelta(days=30, seconds=5)

    def test_add_token_token_persisted_in_yaml(self, tmp_tokens_file: Path) -> None:
        """Token hash is saved to the YAML file; plaintext is not stored."""
        raw, entry = token_service.add_token("read")
        loaded = token_service.load_tokens()
        # entry.token is the hash; all loaded entries store hashes too
        assert any(e.token == entry.token for e in loaded)
        # Plaintext must not appear as any entry's token value
        assert not any(e.token == raw for e in loaded)


class TestTokenEntryIsExpired:
    """TokenEntry.is_expired property."""

    def test_is_expired_false_when_no_expires_at(self) -> None:
        """is_expired is False when expires_at is None."""
        entry = TokenEntry(token="tok", role="read", note=None, expires_at=None)
        assert entry.is_expired is False

    def test_is_expired_false_for_future_expiry(self) -> None:
        """is_expired is False when expires_at is in the future."""
        future = datetime.now(timezone.utc) + timedelta(days=1)
        entry = TokenEntry(token="tok", role="read", note=None, expires_at=future)
        assert entry.is_expired is False

    def test_is_expired_true_for_past_expiry(self) -> None:
        """is_expired is True when expires_at is in the past."""
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        entry = TokenEntry(token="tok", role="read", note=None, expires_at=past)
        assert entry.is_expired is True


class TestLoadTokensExpiresAt:
    """load_tokens() must parse expires_at from YAML."""

    def test_load_tokens_parses_expires_at_string(self, tmp_tokens_file: Path) -> None:
        """load_tokens parses ISO 8601 string into datetime."""
        expires = datetime.now(timezone.utc) + timedelta(days=10)
        data = {
            "tokens": [
                {
                    "token": "abc123",
                    "role": "read",
                    "note": None,
                    "expires_at": expires.isoformat(),
                }
            ]
        }
        tmp_tokens_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        tokens = token_service.load_tokens(tmp_tokens_file)
        assert len(tokens) == 1
        loaded_expiry = tokens[0].expires_at
        assert loaded_expiry is not None
        # Compare as strings since TZ info may differ slightly
        assert abs((loaded_expiry - expires).total_seconds()) < 2

    def test_load_tokens_none_expires_at_when_missing(self, tmp_tokens_file: Path) -> None:
        """load_tokens gives expires_at=None when field is absent."""
        data = {"tokens": [{"token": "tok2", "role": "admin"}]}
        tmp_tokens_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        tokens = token_service.load_tokens(tmp_tokens_file)
        assert tokens[0].expires_at is None


class TestRotateToken:
    """rotate_token() must swap old for new, preserving role/note/expiry."""

    def test_rotate_token_returns_none_for_unknown(self, tmp_tokens_file: Path) -> None:
        """rotate_token returns None when token_value is not found."""
        result = token_service.rotate_token("nonexistent-token")
        assert result is None

    def test_rotate_token_returns_new_raw_and_entry(self, tmp_tokens_file: Path) -> None:
        """rotate_token returns (new_raw, entry) tuple."""
        old_raw, _ = token_service.add_token("run", note="svc")
        result = token_service.rotate_token(old_raw)
        assert result is not None
        new_raw, new_entry = result
        assert isinstance(new_raw, str)
        assert isinstance(new_entry, TokenEntry)

    def test_rotate_token_new_raw_differs_from_old(self, tmp_tokens_file: Path) -> None:
        """The new raw token is different from the old one."""
        old_raw, _ = token_service.add_token("admin")
        new_raw, _ = token_service.rotate_token(old_raw)
        assert new_raw != old_raw

    def test_rotate_token_old_removed_from_store(self, tmp_tokens_file: Path) -> None:
        """The old token can no longer be resolved after rotation."""
        old_raw, _ = token_service.add_token("read")
        token_service.rotate_token(old_raw)
        assert token_service.resolve_token(old_raw) is None

    def test_rotate_token_preserves_role_and_note(self, tmp_tokens_file: Path) -> None:
        """Rotated token keeps the same role and note."""
        old_raw, _ = token_service.add_token("approve", note="my-bot")
        new_raw, new_entry = token_service.rotate_token(old_raw)
        assert new_entry.role == "approve"
        assert new_entry.note == "my-bot"

    def test_rotate_token_preserves_expires_at(self, tmp_tokens_file: Path) -> None:
        """Rotated token carries over the original expires_at."""
        old_raw, old_entry = token_service.add_token("admin", ttl_days=7)
        original_expiry = old_entry.expires_at
        new_raw, new_entry = token_service.rotate_token(old_raw)
        assert new_entry.expires_at == original_expiry

    def test_rotate_token_new_token_resolvable(self, tmp_tokens_file: Path) -> None:
        """The new token can be resolved after rotation; resolved.token is the hash."""
        import hashlib

        old_raw, _ = token_service.add_token("run")
        new_raw, new_entry = token_service.rotate_token(old_raw)
        resolved = token_service.resolve_token(new_raw)
        assert resolved is not None
        # resolved.token holds the hash, not the plaintext
        expected_hash = hashlib.sha256(new_raw.encode()).hexdigest()
        assert resolved.token == expected_hash
        assert resolved.token == new_entry.token
