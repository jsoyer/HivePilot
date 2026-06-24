from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.services import state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

ROLE_RANKS = {"read": 0, "run": 1, "approve": 2, "admin": 3}


def _sha256_hex(value: str) -> str:
    """Return the SHA-256 hex digest of *value*."""
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class TokenEntry:
    token: str  # Stores the SHA-256 hash of the plaintext token
    role: str
    note: str | None = None
    expires_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at


def role_rank(role: str) -> int:
    return ROLE_RANKS.get(role, -1)


def load_tokens(path: Path | None = None) -> list[TokenEntry]:
    resolved = settings.resolve_path(path or settings.tokens_file)
    if not resolved.exists():
        resolved.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    tokens = []
    migrated_count = 0
    rows = list(data.get("tokens", []))
    needs_migration = False

    for entry in rows:
        # Migration: if entry has plaintext `token` but no `token_hash`, hash it now
        if "token" in entry and "token_hash" not in entry:
            entry["token_hash"] = _sha256_hex(entry["token"])
            del entry["token"]
            migrated_count += 1
            needs_migration = True

        raw_expires = entry.get("expires_at")
        if isinstance(raw_expires, str):
            expires_at: datetime | None = datetime.fromisoformat(raw_expires)
        elif isinstance(raw_expires, datetime):
            expires_at = raw_expires
        else:
            expires_at = None
        tokens.append(
            TokenEntry(
                token=entry["token_hash"],
                role=entry["role"],
                note=entry.get("note"),
                expires_at=expires_at,
            )
        )

    if needs_migration:
        logger.info(
            "[token_service] migrated %d legacy token(s) to hashed storage",
            migrated_count,
        )
        # Re-persist the migrated data
        payload = {"tokens": rows}
        resolved.write_text(yaml.safe_dump(payload), encoding="utf-8")

    return tokens


def save_tokens(tokens: list[TokenEntry], path: Path | None = None) -> None:
    resolved = settings.resolve_path(path or settings.tokens_file)
    rows = []
    for entry in tokens:
        row: dict = {
            "token_hash": entry.token,  # entry.token holds the SHA-256 hash
            "role": entry.role,
            "note": entry.note,
        }
        if entry.expires_at is not None:
            row["expires_at"] = entry.expires_at.isoformat()
        rows.append(row)
    payload = {"tokens": rows}
    resolved.write_text(yaml.safe_dump(payload), encoding="utf-8")


def add_token(
    role: str, note: str | None = None, ttl_days: int | None = None
) -> tuple[str, TokenEntry]:
    """Create a new token and return ``(raw_token, entry)``.

    The raw_token is shown ONCE to the caller.  Only the SHA-256 hash is
    persisted on disk or in the state DB.
    """
    plaintext = secrets.token_hex(16)
    token_hash = _sha256_hex(plaintext)
    expires_at: datetime | None = None
    if ttl_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    tokens = load_tokens()
    # Store the hash in the entry (entry.token == hash)
    entry = TokenEntry(token=token_hash, role=role, note=note, expires_at=expires_at)
    tokens.append(entry)
    save_tokens(tokens)
    state_service.store_token(entry)
    logger.info("tokens.added", role=role, note=note)
    return plaintext, entry


def rotate_token(token_value: str) -> tuple[str, TokenEntry] | None:
    """Replace *token_value* with a fresh token keeping the same role/note/expiry.

    Returns ``(new_raw_token, new_entry)`` or ``None`` if the token was not found.
    The raw_token is shown ONCE; only the hash is stored.
    """
    tokens = load_tokens()
    token_hash = _sha256_hex(token_value)
    old = next((e for e in tokens if hmac.compare_digest(e.token, token_hash)), None)
    if old is None:
        return None
    remove_token(token_value)
    new_plaintext = secrets.token_hex(16)
    new_hash = _sha256_hex(new_plaintext)
    new_entry = TokenEntry(
        token=new_hash,
        role=old.role,
        note=old.note,
        expires_at=old.expires_at,
    )
    current = load_tokens()
    current.append(new_entry)
    save_tokens(current)
    state_service.store_token(new_entry)
    logger.info("tokens.rotated", role=new_entry.role)
    return new_plaintext, new_entry


def remove_token(token_value: str) -> bool:
    tokens = load_tokens()
    token_hash = _sha256_hex(token_value)
    filtered = [t for t in tokens if not hmac.compare_digest(t.token, token_hash)]
    if len(filtered) == len(tokens):
        return False
    save_tokens(filtered)
    # state_service indexes tokens by their hash
    state_service.delete_token(token_hash)
    logger.info("tokens.removed")
    return True


def resolve_token(token_value: str) -> TokenEntry | None:
    """Look up a token by its plaintext value.

    Compares sha256(token_value) against stored hashes using constant-time comparison.
    """
    token_hash = _sha256_hex(token_value)
    # Check state cache first (stored by hash)
    row = state_service.get_token(token_hash)
    if row:
        return TokenEntry(token=row["token"], role=row["role"], note=row.get("note"))
    # Fall back to YAML (and cache on hit)
    for entry in load_tokens():
        if hmac.compare_digest(entry.token, token_hash):
            state_service.store_token(entry)
            return entry
    return None
