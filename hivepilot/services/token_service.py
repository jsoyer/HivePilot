from __future__ import annotations

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


@dataclass
class TokenEntry:
    token: str
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
    for entry in data.get("tokens", []):
        raw_expires = entry.get("expires_at")
        if isinstance(raw_expires, str):
            expires_at: datetime | None = datetime.fromisoformat(raw_expires)
        elif isinstance(raw_expires, datetime):
            expires_at = raw_expires
        else:
            expires_at = None
        tokens.append(
            TokenEntry(
                token=entry["token"],
                role=entry["role"],
                note=entry.get("note"),
                expires_at=expires_at,
            )
        )
    return tokens


def save_tokens(tokens: list[TokenEntry], path: Path | None = None) -> None:
    resolved = settings.resolve_path(path or settings.tokens_file)
    rows = []
    for entry in tokens:
        row: dict = {
            "token": entry.token,
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
    """Create a new token and return ``(raw_token, entry)``."""
    token = secrets.token_hex(16)
    expires_at: datetime | None = None
    if ttl_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    tokens = load_tokens()
    entry = TokenEntry(token=token, role=role, note=note, expires_at=expires_at)
    tokens.append(entry)
    save_tokens(tokens)
    state_service.store_token(entry)
    logger.info("tokens.added", role=role, note=note)
    return entry.token, entry


def rotate_token(token_value: str) -> tuple[str, TokenEntry] | None:
    """Replace *token_value* with a fresh token keeping the same role/note/expiry.

    Returns ``(new_raw_token, new_entry)`` or ``None`` if the token was not found.
    """
    tokens = load_tokens()
    old = next((e for e in tokens if e.token == token_value), None)
    if old is None:
        return None
    remove_token(token_value)
    new_hex = secrets.token_hex(16)
    new_entry = TokenEntry(
        token=new_hex,
        role=old.role,
        note=old.note,
        expires_at=old.expires_at,
    )
    current = load_tokens()
    current.append(new_entry)
    save_tokens(current)
    state_service.store_token(new_entry)
    logger.info("tokens.rotated", role=new_entry.role)
    return new_entry.token, new_entry


def remove_token(token_value: str) -> bool:
    tokens = load_tokens()
    filtered = [token for token in tokens if token.token != token_value]
    if len(filtered) == len(tokens):
        return False
    save_tokens(filtered)
    state_service.delete_token(token_value)
    logger.info("tokens.removed", token=token_value)
    return True


def resolve_token(token_value: str) -> TokenEntry | None:
    row = state_service.get_token(token_value)
    if row:
        return TokenEntry(token=row["token"], role=row["role"], note=row.get("note"))
    for entry in load_tokens():
        if entry.token == token_value:
            state_service.store_token(entry)
            return entry
    return None
