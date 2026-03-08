from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any, Dict

from hivepilot.config import settings

_cache: Dict[Path, dict[str, dict[str, Any]]] = {}


def load_claude_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    resolved = settings.resolve_path(path or settings.claude_profiles_file)
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
