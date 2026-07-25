"""ForgeProvider — the git-forge plugin type (Phase 1 of the forge plugin
type PRD: extract a provider abstraction and make GitHub the default,
behaviour-preserving). Mirrors hivepilot.registry's RunnerRegistry/
SecretsRegistry pattern: a process-global FORGE_MAP name -> provider
instance, plus a fail-closed resolve_forge(project) lookup.

Phase 1 shipped ONE concrete provider (GitHubForge, registered as "github"
-- see hivepilot/forges/github.py). Phase 2 (this module's additions below
GitHubForge's original content) adds two self-hosted providers -- Forgejo
(hivepilot/forges/forgejo.py) and GitLab (hivepilot/forges/gitlab.py) --
plus the shared helpers both need: HTTPS enforcement for a self-hosted
`forge_base_url`, and fail-closed `${secret:NAME}` token resolution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

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

    def build_repo_url(
        self, repo: str, protocol: str, project: ProjectConfig | None = None
    ) -> str: ...

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


# ---------------------------------------------------------------------------
# Phase 2 shared helpers -- used by ForgejoForge (forgejo.py) and GitLabForge
# (gitlab.py), the two self-hosted-capable providers. GitHubForge doesn't need
# these: github.com is a single fixed, always-https host with no per-project
# token catalog entry of its own (it authenticates via the `gh` CLI's own
# stored credentials).
# ---------------------------------------------------------------------------

# Mirrors hivepilot.runners.worker_runner._LOOPBACK_HOSTS -- the one carve-out
# for a plaintext http:// forge_base_url (local dev against a forge running on
# the same machine).
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_secure_forge_url(base_url: str) -> None:
    """Refuse a plaintext ``http://`` *base_url* to a non-loopback host --
    mirrors ``hivepilot.runners.worker_runner._require_secure_transport``. A
    self-hosted forge's API token must never travel over unencrypted
    transport to a real host; only a loopback ``http://`` (local dev) is
    exempt. Also rejects any scheme other than http/https outright.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"forge_base_url must be http:// or https://; got {base_url!r}")
    if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing plaintext http:// forge_base_url to non-loopback host "
            f"{parsed.hostname!r}; use https:// (or a loopback host for local dev)."
        )


def resolve_forge_base_url(project: ProjectConfig, *, default: str | None = None) -> str:
    """Resolve *project*'s ``forge_base_url``, enforcing HTTPS.

    Fail-closed when unset and no *default* is given (Forgejo: there is no
    universal public Forgejo host the way github.com/gitlab.com exist, so an
    unset ``forge_base_url`` is a configuration error, never a silent
    fallback). Falls back to *default* when one is given (GitLab: the public
    ``https://gitlab.com`` SaaS) -- an explicit, documented default, not an
    accidental one.
    """
    base_url = project.forge_base_url or default
    if not base_url:
        raise ValueError(
            f"Project {project.path.name!r} uses forge {project.forge!r} but has no "
            f"forge_base_url configured; set forge_base_url in projects.yaml."
        )
    require_secure_forge_url(base_url)
    return base_url.rstrip("/")


def resolve_forge_token(project: ProjectConfig, ref_name: str) -> str:
    """Resolve *ref_name* (e.g. ``"FORGEJO_TOKEN"``) through the EXISTING
    ``${secret:NAME}`` mechanism (``hivepilot.services.secret_refs``) against
    *project*'s ``secrets:`` catalog -- never read a plaintext environment
    variable directly.

    Fail-closed: a missing catalog entry or a provider resolution failure
    both raise ``SecretReferenceError`` (from ``resolve_secret_refs`` itself);
    an EMPTY resolved value also raises here -- an empty token must never be
    silently treated as "no auth needed" (see the empty-value fail-open
    lesson this codebase has hit before: an empty/absent value on a gate must
    mean deny, not permit). The resolved value is registered for redaction by
    ``resolve_secret_refs`` itself.
    """
    from hivepilot.services.secret_refs import resolve_secret_refs

    values = {ref_name: f"${{secret:{ref_name}}}"}
    resolved = resolve_secret_refs(values, catalog=project.secrets, fail_mode="closed")
    token = resolved[ref_name]
    if not token or not token.strip():
        raise ValueError(
            f"Project {project.path.name!r} secret {ref_name!r} resolved to an empty "
            f"value; refusing to authenticate with an empty token."
        )
    return token
