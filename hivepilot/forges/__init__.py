"""hivepilot.forges plugin type: GitHub default provider (Phase 1)."""

from __future__ import annotations

from hivepilot.forges.github import GitHubForge  # registers "github" into FORGE_MAP
from hivepilot.forges.provider import (
    FORGE_MAP,
    ForgeCollisionError,
    ForgeProvider,
    ForgeRegistry,
    UnknownForgeError,
    resolve_forge,
)

__all__ = [
    "FORGE_MAP",
    "ForgeCollisionError",
    "ForgeProvider",
    "ForgeRegistry",
    "GitHubForge",
    "UnknownForgeError",
    "resolve_forge",
]
