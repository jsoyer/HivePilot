"""
Tests for SHA-256 hashing of API tokens at rest (PROD-HARDENING 2b).

Verifies:
- add_token returns plaintext, only stores a hash
- resolve_token(plaintext) succeeds via constant-time comparison
- Wrong token is rejected
- Stored YAML contains token_hash, NOT a plaintext token field
- Legacy plaintext entries are migrated on load and original value still resolves
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

import hivepilot.services.state_service as state_service
import hivepilot.services.token_service as token_service


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


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TestAddTokenHashing:
    """add_token() must return plaintext and store only a hash."""

    def test_add_token_returns_plaintext_not_hash(self, tmp_tokens_file: Path) -> None:
        """add_token returns the raw 32-char hex token (not its hash)."""
        plaintext, entry = token_service.add_token("admin")
        # Plaintext is a 32-char hex string (secrets.token_hex(16))
        assert len(plaintext) == 32
        assert all(c in "0123456789abcdef" for c in plaintext)

    def test_add_token_entry_token_is_hash(self, tmp_tokens_file: Path) -> None:
        """entry.token must be the sha256 hash of the returned plaintext."""
        plaintext, entry = token_service.add_token("admin")
        assert entry.token == _sha256_hex(plaintext)
        # Hash is 64 hex chars
        assert len(entry.token) == 64

    def test_add_token_plaintext_not_equal_to_entry_token(self, tmp_tokens_file: Path) -> None:
        """The returned plaintext must differ from the stored hash."""
        plaintext, entry = token_service.add_token("run")
        assert plaintext != entry.token

    def test_yaml_contains_token_hash_not_plaintext(self, tmp_tokens_file: Path) -> None:
        """Stored YAML must have token_hash field, not a plaintext token field."""
        plaintext, _ = token_service.add_token("read")
        data = yaml.safe_load(tmp_tokens_file.read_text(encoding="utf-8"))
        rows = data["tokens"]
        assert len(rows) == 1
        row = rows[0]
        assert "token_hash" in row
        assert "token" not in row
        # The stored hash must equal sha256(plaintext)
        assert row["token_hash"] == _sha256_hex(plaintext)

    def test_yaml_does_not_contain_plaintext_value(self, tmp_tokens_file: Path) -> None:
        """The plaintext must not appear anywhere in the YAML file."""
        plaintext, _ = token_service.add_token("admin")
        raw_yaml = tmp_tokens_file.read_text(encoding="utf-8")
        assert plaintext not in raw_yaml


class TestResolveTokenHashing:
    """resolve_token() must work against plaintext, not hash."""

    def test_resolve_token_with_plaintext_succeeds(self, tmp_tokens_file: Path) -> None:
        """Presenting the original plaintext resolves to a TokenEntry."""
        plaintext, _ = token_service.add_token("run")
        resolved = token_service.resolve_token(plaintext)
        assert resolved is not None

    def test_resolve_token_role_matches(self, tmp_tokens_file: Path) -> None:
        """Resolved entry has the correct role."""
        plaintext, _ = token_service.add_token("approve", note="ci-bot")
        resolved = token_service.resolve_token(plaintext)
        assert resolved is not None
        assert resolved.role == "approve"
        assert resolved.note == "ci-bot"

    def test_resolve_wrong_token_returns_none(self, tmp_tokens_file: Path) -> None:
        """Presenting an invalid token returns None."""
        token_service.add_token("read")
        assert token_service.resolve_token("wrong-token-value") is None

    def test_resolve_hash_string_returns_none(self, tmp_tokens_file: Path) -> None:
        """Presenting the hash itself (not the plaintext) must fail."""
        plaintext, entry = token_service.add_token("admin")
        # entry.token IS the hash — it should NOT resolve
        assert token_service.resolve_token(entry.token) is None


class TestLegacyMigration:
    """Legacy YAML entries with plaintext token field are migrated on load."""

    def test_migration_hashes_legacy_entry(self, tmp_tokens_file: Path) -> None:
        """Loading a legacy plaintext entry converts it to token_hash on disk."""
        legacy_plaintext = "deadbeef" * 4  # 32-char hex
        data = {
            "tokens": [
                {
                    "token": legacy_plaintext,
                    "role": "read",
                    "note": "legacy-bot",
                }
            ]
        }
        tmp_tokens_file.write_text(yaml.safe_dump(data), encoding="utf-8")

        # Trigger migration via load_tokens
        tokens = token_service.load_tokens(tmp_tokens_file)

        # In-memory: entry.token is now the hash
        assert len(tokens) == 1
        assert tokens[0].token == _sha256_hex(legacy_plaintext)

        # On disk: token_hash present, plaintext token gone
        persisted = yaml.safe_load(tmp_tokens_file.read_text(encoding="utf-8"))
        row = persisted["tokens"][0]
        assert "token_hash" in row
        assert "token" not in row
        assert row["token_hash"] == _sha256_hex(legacy_plaintext)

    def test_legacy_plaintext_still_resolves_after_migration(
        self, tmp_tokens_file: Path
    ) -> None:
        """After migration, the original plaintext still resolves correctly."""
        legacy_plaintext = "cafebabe" * 4  # 32-char hex


        data = {
            "tokens": [
                {
                    "token": legacy_plaintext,
                    "role": "run",
                    "note": "svc",
                }
            ]
        }
        tmp_tokens_file.write_text(yaml.safe_dump(data), encoding="utf-8")

        # Redirect settings so resolve_token uses the right file
        import hivepilot.services.token_service as ts

        resolved = ts.resolve_token(legacy_plaintext)
        assert resolved is not None
        assert resolved.role == "run"

    def test_no_migration_for_already_hashed_entry(self, tmp_tokens_file: Path) -> None:
        """Entries with token_hash but no token field are not double-migrated."""
        plaintext = "aabbccdd" * 4
        existing_hash = _sha256_hex(plaintext)
        data = {
            "tokens": [
                {
                    "token_hash": existing_hash,
                    "role": "admin",
                }
            ]
        }
        tmp_tokens_file.write_text(yaml.safe_dump(data), encoding="utf-8")

        tokens = token_service.load_tokens(tmp_tokens_file)
        assert len(tokens) == 1
        assert tokens[0].token == existing_hash

        # File should be unchanged (no migration needed)
        persisted = yaml.safe_load(tmp_tokens_file.read_text(encoding="utf-8"))
        row = persisted["tokens"][0]
        assert row["token_hash"] == existing_hash
        assert "token" not in row


class TestRotateTokenHashing:
    """rotate_token() must use hash-based lookup and return plaintext."""

    def test_rotate_token_returns_plaintext(self, tmp_tokens_file: Path) -> None:
        """rotate_token returns raw 32-char plaintext, not a hash."""
        old_plain, _ = token_service.add_token("run")
        result = token_service.rotate_token(old_plain)
        assert result is not None
        new_plain, new_entry = result
        assert len(new_plain) == 32
        assert all(c in "0123456789abcdef" for c in new_plain)

    def test_rotate_token_new_entry_stores_hash(self, tmp_tokens_file: Path) -> None:
        """After rotation, the YAML has token_hash for the new entry."""
        old_plain, _ = token_service.add_token("admin")
        new_plain, _ = token_service.rotate_token(old_plain)

        data = yaml.safe_load(tmp_tokens_file.read_text(encoding="utf-8"))
        rows = data["tokens"]
        assert len(rows) == 1
        row = rows[0]
        assert "token_hash" in row
        assert "token" not in row
        assert row["token_hash"] == _sha256_hex(new_plain)

    def test_rotate_token_old_no_longer_resolves(self, tmp_tokens_file: Path) -> None:
        """Old token cannot be resolved after rotation."""
        old_plain, _ = token_service.add_token("read")
        token_service.rotate_token(old_plain)
        assert token_service.resolve_token(old_plain) is None

    def test_rotate_token_new_resolves(self, tmp_tokens_file: Path) -> None:
        """New token can be resolved after rotation."""
        old_plain, _ = token_service.add_token("approve")
        new_plain, _ = token_service.rotate_token(old_plain)
        resolved = token_service.resolve_token(new_plain)
        assert resolved is not None
