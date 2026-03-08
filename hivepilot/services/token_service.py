from __future__ import annotations

import secrets
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import List

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


def role_rank(role: str) -> int:
    return ROLE_RANKS.get(role, -1)


def load_tokens(path: Path | None = None) -> list[TokenEntry]:
    resolved = settings.resolve_path(path or settings.tokens_file)
    if not resolved.exists():
        resolved.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    tokens = []
    for entry in data.get("tokens", []):
        tokens.append(TokenEntry(token=entry["token"], role=entry["role"], note=entry.get("note")))
    return tokens


def save_tokens(tokens: list[TokenEntry], path: Path | None = None) -> None:
    resolved = settings.resolve_path(path or settings.tokens_file)
    payload = {"tokens": [entry.__dict__ for entry in tokens]}
    resolved.write_text(yaml.safe_dump(payload), encoding="utf-8")


def add_token(role: str, note: str | None = None) -> TokenEntry:
    token = secrets.token_hex(16)
    tokens = load_tokens()
    entry = TokenEntry(token=token, role=role, note=note)
    tokens.append(entry)
    save_tokens(tokens)
    state_service.store_token(entry)
    logger.info("tokens.added", role=role, note=note)
    return entry


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
