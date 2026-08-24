"""Named roster presets — the vendor switch.

``roles.yaml`` stays the canonical Claude roster. A preset is an overlay of
``{role: {runner, model}}`` selected by ``HIVEPILOT_ROSTER_PRESET`` (default
``claude`` = identity). Switching back is one env var, not a rewrite.

Files resolve like every other config file:
``roster-presets/<name>.yaml`` via ``settings.resolve_config_path``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: Agent CLIs that may overlay each other when a preset/policy changes kind.
#: Non-agent kinds (shell, herdr, terraform, …) must never be rewritten.
AGENT_KINDS: frozenset[str] = frozenset(
    {
        "claude",
        "grok",
        "cursor",
        "codex",
        "vibe",
        "openrouter",
        "gemini",
        "opencode",
        "pi",
        "qwen",
        "kimi",
        "ollama",
        "antigravity",
    }
)

_PRINT_FULL_TOOLS: frozenset[str] = frozenset({"cursor", "codex"})

_cache: dict[tuple[str, str], "RosterPreset"] = {}


@dataclass(frozen=True)
class RosterPreset:
    name: str
    roles: dict[str, dict[str, str]] = field(default_factory=dict)
    judge_runner: str | None = None
    judge_model: str | None = None
    lesson_distill_runner: str | None = None
    lesson_distill_model: str | None = None

    def override_for(self, role_name: str) -> dict[str, str]:
        return dict(self.roles.get(role_name) or {})


def _preset_path(name: str) -> Path:
    return settings.resolve_config_path(Path("roster-presets") / f"{name}.yaml")


def load_roster_preset(name: str | None = None) -> RosterPreset:
    """Load the named preset. ``claude`` (default) with no file is identity.

    An unknown *other* name fails closed: a typo must not silently run Claude.
    """
    preset_name = (name if name is not None else settings.roster_preset) or "claude"
    preset_name = str(preset_name).strip() or "claude"
    cache_key = (preset_name, str(settings.base_dir))
    if cache_key in _cache:
        return _cache[cache_key]

    path = _preset_path(preset_name)
    if not path.exists():
        if preset_name == "claude":
            preset = RosterPreset(name="claude")
            _cache[cache_key] = preset
            return preset
        raise FileNotFoundError(
            f"roster preset {preset_name!r} not found at {path} "
            f"(searched XDG → config-repo → base_dir). "
            f"Create roster-presets/{preset_name}.yaml or set "
            f"HIVEPILOT_ROSTER_PRESET=claude."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"roster preset {preset_name!r} must be a mapping, got {type(raw)}")
    roles_raw = (
        raw.get("roles")
        if "roles" in raw
        else {k: v for k, v in raw.items() if k != "judge" and k != "lessons"}
    )
    if roles_raw is None:
        roles_raw = {}
    if not isinstance(roles_raw, dict):
        raise ValueError(f"roster preset {preset_name!r} 'roles' must be a mapping")
    roles: dict[str, dict[str, str]] = {}
    for role_name, spec in roles_raw.items():
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"roster preset {preset_name!r} role {role_name!r} must be a mapping")
        roles[str(role_name)] = {str(k): str(v) for k, v in spec.items() if v is not None}

    judge = raw.get("judge") if isinstance(raw.get("judge"), dict) else {}
    lessons = raw.get("lessons") if isinstance(raw.get("lessons"), dict) else {}
    preset = RosterPreset(
        name=preset_name,
        roles=roles,
        judge_runner=judge.get("runner"),
        judge_model=judge.get("model"),
        lesson_distill_runner=lessons.get("runner"),
        lesson_distill_model=lessons.get("model"),
    )
    _cache[cache_key] = preset
    logger.info("roster.preset_loaded", name=preset_name, path=str(path), roles=len(roles))
    return preset


def clear_roster_preset_cache() -> None:
    _cache.clear()


def drop_unhonoured_bypass(kind: str, options: dict[str, Any]) -> dict[str, Any]:
    """Cursor/Codex ``--print`` is already fully armed. ``bypassPermissions``
    on those kinds would be refused by ``assert_runner_honours``. Drop only
    that one flag; ``allowed_tools`` still refuses.
    """
    if kind not in _PRINT_FULL_TOOLS:
        return options
    mode = options.get("permission_mode")
    if mode != "bypassPermissions":
        return options
    out = dict(options)
    out.pop("permission_mode", None)
    logger.warning(
        "runner.permission_mode_dropped",
        kind=kind,
        dropped="bypassPermissions",
        reason="runner print mode is already fully armed; honour-controls would refuse the role",
    )
    return out


def resolved_roster_rows(
    role_names: list[str], policy: object | None = None
) -> list[tuple[str, str, str | None]]:
    """``(role, runner, model)`` for doctor. Imports ``resolve_runner`` lazily
    to avoid a cycle at module import.
    """
    from hivepilot.roles import resolve_runner

    rows: list[tuple[str, str, str | None]] = []
    for name in role_names:
        try:
            runner, model, _effort = resolve_runner(name, policy)
        except Exception as exc:  # noqa: BLE001 — doctor must list what it can
            rows.append((name, f"ERROR: {exc}", None))
            continue
        rows.append((name, runner or "?", model))
    return rows
