"""ForgeProvider — the git-forge plugin type (Phase 1 of the forge plugin
type PRD: extract a provider abstraction and make GitHub the default,
behaviour-preserving). Mirrors hivepilot.registry's RunnerRegistry/
SecretsRegistry pattern: a process-global FORGE_MAP name -> provider
instance, plus a fail-closed resolve_forge(project) lookup.

Only ONE concrete provider ships in Phase 1 (GitHubForge, registered as
"github" -- see hivepilot/forges/github.py). Forgejo/GitLab providers and
instance federation are later phases; this module's job is just the seam
they will plug into.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hivepilot.config import Settings
from hivepilot.models import GitActions, ProjectConfig


class ForgeProvider(Protocol):
    """Structural interface for a hosted git-forge integration.

    Models the COMMON concept across forges -- a repo/issue/release
    lifecycle plus a "change request" (GitHub pull request / GitLab merge
    request / Forgejo pull request) lifecycle -- rather than GitHub-specific
    field names, so a later Forgejo/GitLab provider can implement this same
    surface.
    """

    name: str

    def build_repo_url(self, repo: str, protocol: str) -> str: ...

    def repo_exists(self, slug: str, settings: Settings, project: ProjectConfig) -> bool: ...

    def create_repo(
        self,
        slug: str,
        *,
        settings: Settings,
        project: ProjectConfig,
        visibility: str,
        description: str | None,
    ) -> None: ...

    def ensure_repository(
        self,
        project: ProjectConfig,
        settings: Settings,
        *,
        push: bool,
        set_remote: bool = True,
        remote_protocol: str = "ssh",
        visibility: str = "private",
    ) -> None: ...

    def create_issue(
        self,
        *,
        project: ProjectConfig,
        settings: Settings,
        title: str,
        body: str | None,
        labels: list[str],
    ) -> None: ...

    def create_release(
        self,
        *,
        project: ProjectConfig,
        settings: Settings,
        tag: str,
        title: str | None,
        notes_file: Path | None = None,
        generate_notes: bool = True,
    ) -> None: ...

    def open_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None: ...

    def promote_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None: ...

    def comment_pr(self, *, project: ProjectConfig, branch: str, body: str) -> None: ...

    def pr_status(self, *, project: ProjectConfig, branch: str) -> str: ...

    def merge_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None: ...


class UnknownForgeError(RuntimeError):
    """Raised by resolve_forge when a project's `forge` name isn't (or is no
    longer) registered in FORGE_MAP. Fail-closed -- an unrecognized forge
    must NEVER silently fall back to GitHub. `ProjectConfig.forge`'s own
    field validator already rejects an unknown name at config-load time (see
    hivepilot/models.py); this is the runtime-side belt-and-suspenders twin
    of that check, for the (currently theoretical -- Phase 1 has no
    unregister path) case where a provider disappears from FORGE_MAP after
    load.
    """


class ForgeCollisionError(RuntimeError):
    """Raised by ForgeRegistry.register when a DIFFERENT provider instance
    tries to silently replace an already-registered name (mirrors
    RunnerKindCollisionError / SecretsBackendCollisionError in
    hivepilot.registry)."""


FORGE_MAP: dict[str, ForgeProvider] = {}


class ForgeRegistry:
    """Process-global forge registry -- mirrors RunnerRegistry/SecretsRegistry."""

    @staticmethod
    def register(name: str, provider: ForgeProvider, *, override: bool = False) -> None:
        if name in FORGE_MAP and FORGE_MAP[name] is not provider and not override:
            raise ForgeCollisionError(
                f"Forge '{name}' is already registered to "
                f"{type(FORGE_MAP[name]).__name__}; refusing to silently replace it "
                f"with {type(provider).__name__}"
            )
        FORGE_MAP[name] = provider

    @staticmethod
    def known_kinds() -> frozenset[str]:
        return frozenset(FORGE_MAP)


def resolve_forge(project: ProjectConfig) -> ForgeProvider:
    """Resolve *project*'s configured forge provider from FORGE_MAP.

    Fail-closed: raises UnknownForgeError (never a silent GitHub fallback) if
    project.forge isn't a registered provider name.
    """
    provider = FORGE_MAP.get(project.forge)
    if provider is None:
        raise UnknownForgeError(
            f"Unknown forge {project.forge!r} for project {project.path.name!r}; "
            f"available: {sorted(FORGE_MAP)}"
        )
    return provider
