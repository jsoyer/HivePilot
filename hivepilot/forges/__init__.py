"""hivepilot.forges plugin type: GitHub default provider (Phase 1) + the
self-hosted Forgejo/GitLab providers (Phase 2).

Forgejo/GitLab (forgejo.py/gitlab.py) import `httpx` at module level, which
is only an OPTIONAL extra (`pip install hivepilot[forge]`), not a core
dependency (see pyproject.toml) -- so their registration import here is
guarded by a try/except ImportError. When httpx isn't installed, "forgejo"/
"gitlab" simply aren't added to FORGE_MAP: a project that declares one still
fails closed (a clear "unknown forge" error, from `ProjectConfig`'s own
fail-closed validator / `resolve_forge`), it just does so at CONFIG time
instead of at `import hivepilot.forges` time -- which is what lets every
`ProjectConfig` construction (`forge: "github"`, the default, needs none of
this) keep working on a core install with zero new dependencies.
"""

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

try:
    from hivepilot.forges.forgejo import ForgejoForge  # registers "forgejo" into FORGE_MAP
except ImportError:  # pragma: no cover -- exercised by CI's default (httpx installed) env
    ForgejoForge = None  # type: ignore[assignment, misc]

try:
    from hivepilot.forges.gitlab import GitLabForge  # registers "gitlab" into FORGE_MAP
except ImportError:  # pragma: no cover -- exercised by CI's default (httpx installed) env
    GitLabForge = None  # type: ignore[assignment, misc]

__all__ = [
    "FORGE_MAP",
    "ForgeCollisionError",
    "ForgeProvider",
    "ForgeRegistry",
    "ForgejoForge",
    "GitHubForge",
    "GitLabForge",
    "UnknownForgeError",
    "resolve_forge",
]
