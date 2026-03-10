from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Policy:
    allow_auto_git: bool = True
    require_approval: bool = False
    allow_containers: bool = True


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_policies(path: Path | None = None) -> dict:
    resolved = settings.resolve_config_path(path or settings.policies_file)
    return _load_yaml(resolved)


def reload_policies() -> None:
    """Invalidate the policies cache so the next call re-reads the file."""
    _cache.clear()


# Internal cache — populated on first use, cleared by reload_policies()
_cache: dict = {}


def _get_policies() -> dict:
    if "data" not in _cache:
        _cache["data"] = load_policies(settings.policies_file)
    return _cache["data"]


def get_policy(project_name: str) -> Policy:
    policies = _get_policies()
    project_rules = policies.get("projects", {}).get(project_name) if policies else None
    default = policies.get("default", {}) if policies else {}
    rules = {**default, **(project_rules or {})}
    return Policy(
        allow_auto_git=rules.get("allow_auto_git", True),
        require_approval=rules.get("require_approval", False),
        allow_containers=rules.get("allow_containers", True),
    )


def enforce_policy(project_name: str, *, auto_git: bool) -> Policy:
    policy = get_policy(project_name)
    if auto_git and not policy.allow_auto_git:
        raise RuntimeError(f"Auto-git is disabled by policy for project {project_name}")
    return policy
