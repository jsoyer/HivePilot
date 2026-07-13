from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import yaml

from hivepilot.config import settings

logger = logging.getLogger(__name__)

_cache: Dict[Path, dict[str, dict[str, Any]]] = {}


def _warn_if_stray_config_copy(resolved: Path) -> None:
    """Warn if a dead config/model_profiles.yaml sits next to the resolved
    file. model_profiles.yaml has exactly one source of truth (whatever
    resolve_config_path resolves to); a stray copy in a sibling `config/`
    directory must never be silently read instead."""
    stray = resolved.parent / "config" / resolved.name
    try:
        exists = stray.exists()
    except OSError:
        return
    if not exists:
        return
    try:
        if stray.resolve() == resolved.resolve():
            return
    except OSError:
        pass
    logger.warning(
        "stray config/model_profiles.yaml ignored — root is the source of truth "
        "(stray=%s, resolved=%s)",
        stray,
        resolved,
    )


def load_claude_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    resolved = settings.resolve_config_path(path or settings.claude_profiles_file)
    _warn_if_stray_config_copy(resolved)
    if resolved in _cache:
        return _cache[resolved]
    if not resolved.exists():
        data: dict[str, dict[str, Any]] = {}
    else:
        with resolved.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        data = raw.get("claude_profiles", {})
    _cache[resolved] = data
    return data
