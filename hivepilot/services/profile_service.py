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


def load_model_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Profiles from `model_profiles.yaml`, reading BOTH spellings.

    `model_profiles:` is the generic key (#28 — per-runner entries like
    `architecture: {claude: opus, grok: grok-4.6}`). `claude_profiles:` is the
    legacy key every deployed file carries, because the init template wrote
    it; dropping it would silently zero their profiles, which is the exact
    defect #28 removes. On a name clash the generic entry wins.
    """
    resolved = settings.resolve_config_path(path or settings.claude_profiles_file)
    _warn_if_stray_config_copy(resolved)
    if resolved in _cache:
        return _cache[resolved]
    if not resolved.exists():
        data: dict[str, dict[str, Any]] = {}
    else:
        with resolved.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        data = {**raw.get("claude_profiles", {}), **raw.get("model_profiles", {})}
    _cache[resolved] = data
    return data


#: The old name, kept as an alias: cli.py and claude_runner import it, and the
#: profiles were never claude's alone — only their READER was.
def load_claude_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    return load_model_profiles(path)


def resolve_profile_model(profile: str | None, runner_kind: str) -> str | None:
    """The model *profile* names for *runner_kind*, or None with a WARNING.

    None always means "fall back to the role's flat `model:`" — the fallback
    is legitimate; the silence around it was not. Measured on the box before
    this existed: the loader returned 0 profiles while every noxys role
    declared one, and `role.model_profile` was consumed nowhere in dispatch.
    The profile system had never done anything, twice over, and nobody knew.

    A legacy `{model: X}` entry counts for claude ONLY: profiles were only
    ever consulted by ClaudeRunner, and values like `opus` are claude
    vocabulary — handing them to grok would name a model it cannot serve.
    """
    if not profile:
        # Declaring no profile is not a mistake; silence is correct here.
        return None
    profiles = load_model_profiles()
    entry = profiles.get(profile)
    if entry is None:
        logger.warning(
            "model_profile %r is declared but not defined in model_profiles.yaml "
            "— falling back to the role's flat `model:`. Define the profile (or "
            "remove the declaration) to end this warning.",
            profile,
        )
        return None
    value = entry.get(runner_kind)
    if value:
        return str(value)
    if runner_kind == "claude" and entry.get("model"):
        return str(entry["model"])
    logger.warning(
        "model_profile %r has no entry for runner %r — falling back to the "
        "role's flat `model:`. Add `%s: <model>` under that profile to route it.",
        profile,
        runner_kind,
        runner_kind,
    )
    return None
