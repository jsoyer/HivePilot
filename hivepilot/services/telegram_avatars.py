"""Per-role Telegram avatars (HP-16).

Hand-off messages prefix a role emoji. The eight first-class roster roles
always get a Unicode fallback (works without Premium). When the operator
has uploaded a custom-emoji sticker set (Bot API 9.4; bot owner must have
Telegram Premium) and mapped ``custom_emoji_id`` values in
``telegram_avatars.yaml``, HTML cards wrap the same emoji in ``<tg-emoji>``
and the plain-text path attaches a ``custom_emoji`` MessageEntity.

This module never talks to Telegram and never invents sticker IDs. A
missing file, a disabled flag, or a rejected send all degrade to the
Unicode map.
"""

from __future__ import annotations

import html
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Same eight keys as ``web/src/lib/role-avatars.ts`` (HP-20). Artwork is
# shared conceptually; these are the alt/fallback glyphs used in chat.
CANONICAL_ROLE_EMOJI: dict[str, str] = {
    "ceo": "👑",
    "chief_of_staff": "📋",
    "cto": "🧭",
    "developer": "🛠️",
    "reviewer": "🔍",
    "ciso": "🛡️",
    "qa": "🧪",
    "documentation": "📝",
}

_RESERVED_KEYS = frozenset({"sticker_set", "avatars"})

# path -> (mtime, mapping)
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def reset_cache() -> None:
    """Drop the on-disk mapping cache (tests)."""
    _cache.clear()


def utf16_len(text: str) -> int:
    """Telegram MessageEntity offsets are UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def fallback_emoji(role_key: str | None) -> str:
    """Unicode glyph for *role_key*, or ``\"\"`` when the role is unknown."""
    if not role_key:
        return ""
    return CANONICAL_ROLE_EMOJI.get(role_key, "")


def custom_emoji_enabled() -> bool:
    return bool(getattr(settings, "telegram_custom_emoji", True))


def custom_emoji_id(role_key: str | None) -> str | None:
    """Configured custom-emoji id, or ``None`` when gated off / missing."""
    if not role_key or not custom_emoji_enabled():
        return None
    raw = load_custom_emoji_ids().get(role_key)
    return raw or None


def role_key_from_actor(actor: str) -> str | None:
    """Map an actor display string to a ``roles.yaml`` key.

    Same display_name / title match as the stream topic resolver, but
    without minting a topic slug and without logging unmatched actors —
    an unknown speaker simply gets no role avatar.
    """
    from hivepilot.roles import ROLES

    if not actor or not ROLES:
        return None
    actor_norm = _normalize(actor)
    for key, role in ROLES.items():
        if role.display_name and _normalize(role.display_name) in actor_norm:
            return key
        if role.title and _normalize(role.title) in actor_norm:
            return key
    if actor_norm in ROLES:
        return actor_norm
    return None


def html_mark(role_key: str | None) -> str:
    """Avatar markup plus trailing space, or ``\"\"``.

    With a configured id: ``<tg-emoji emoji-id=\"…\">👑</tg-emoji> ``.
    Otherwise the Unicode fallback (or empty for an unknown role).
    """
    emoji = fallback_emoji(role_key)
    if not emoji:
        return ""
    emoji_id = custom_emoji_id(role_key)
    if emoji_id:
        safe_id = html.escape(emoji_id, quote=True)
        return f'<tg-emoji emoji-id="{safe_id}">{emoji}</tg-emoji> '
    return f"{emoji} "


def plain_mark(role_key: str | None) -> str:
    """Unicode avatar plus trailing space, or ``\"\"``."""
    emoji = fallback_emoji(role_key)
    return f"{emoji} " if emoji else ""


def custom_emoji_entities(role_key: str | None, *, prefix: str = "") -> list[dict[str, Any]] | None:
    """MessageEntity list wrapping the fallback emoji after *prefix*, or ``None``."""
    emoji = fallback_emoji(role_key)
    emoji_id = custom_emoji_id(role_key)
    if not emoji or not emoji_id:
        return None
    return [
        {
            "type": "custom_emoji",
            "offset": utf16_len(prefix),
            "length": utf16_len(emoji),
            "custom_emoji_id": emoji_id,
        }
    ]


def load_custom_emoji_ids() -> dict[str, str]:
    """Role → digit-only ``custom_emoji_id``. Missing/invalid file → ``{}``."""
    path = settings.resolve_config_path(settings.telegram_avatars_file)
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cache_key = str(path)
    cached = _cache.get(cache_key)
    if cached and cached[0] == mtime:
        return dict(cached[1])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram.avatars.load_failed", path=str(path), error=str(exc))
        return {}
    mapping = parse_avatar_ids(data)
    _cache[cache_key] = (mtime, mapping)
    return dict(mapping)


def parse_avatar_ids(data: Any) -> dict[str, str]:
    """Accept a flat map or ``{avatars: {role: id| {custom_emoji_id}}}``."""
    if not isinstance(data, dict):
        return {}
    raw = data.get("avatars", data)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name or name in _RESERVED_KEYS:
            continue
        extracted = _extract_id(value)
        if extracted:
            out[name] = extracted
    return out


def validate_telegram_avatars_file(path: Path) -> list[str]:
    """Lint an optional ``telegram_avatars.yaml``. Missing file is not an error."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"telegram_avatars.yaml: YAML parse error: {exc}"]
    if data is None:
        return []
    if not isinstance(data, dict):
        return ["telegram_avatars.yaml: expected a mapping of role → custom_emoji_id"]
    raw = data.get("avatars", data)
    if not isinstance(raw, dict):
        return ["telegram_avatars.yaml: 'avatars' must be a mapping"]

    known = set(CANONICAL_ROLE_EMOJI)
    try:
        from hivepilot.roles import ROLES

        known |= set(ROLES)
    except Exception:  # noqa: BLE001
        pass

    problems: list[str] = []
    for key, value in raw.items():
        name = str(key).strip()
        if not name or name in _RESERVED_KEYS:
            continue
        if name not in known:
            problems.append(f"telegram_avatars.yaml: unknown role '{name}'")
        extracted = _extract_id(value)
        if value in (None, "") or (isinstance(value, dict) and not value):
            continue
        if extracted is None:
            problems.append(
                f"telegram_avatars.yaml: role '{name}' custom_emoji_id must be digits, "
                f"got {value!r}"
            )
    return problems


def _extract_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("custom_emoji_id", value.get("id"))
    if value in (None, ""):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    text = str(value).strip()
    if text.isdigit():
        return text
    return None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()
