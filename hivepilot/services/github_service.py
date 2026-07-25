"""Thin GitHub-forge wrappers (Phase 1 of the forge plugin type PRD).

Every function here used to shell out to `gh` directly; now each one
resolves the project's configured forge provider
(`hivepilot.forges.resolve_forge` -- "github" by default, the only forge
Phase 1 ships) and delegates. For a `forge: "github"` project (every
pre-existing project, since that's the default) this is byte-identical to
the old inline implementation, which now lives in
`hivepilot.forges.github.GitHubForge`.

Kept as its own module (rather than deleting it and updating every caller)
because `hivepilot.cli` / `hivepilot.runners.internal_runner` import these
names directly -- this preserves that public surface unchanged.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import Settings
from hivepilot.forges import FORGE_MAP, resolve_forge
from hivepilot.models import ProjectConfig


def repo_exists(slug: str, settings: Settings, project: ProjectConfig) -> bool:
    return resolve_forge(project).repo_exists(slug, settings, project)


def create_repo(
    slug: str,
    *,
    settings: Settings,
    project: ProjectConfig,
    visibility: str,
    description: str | None,
) -> None:
    resolve_forge(project).create_repo(
        slug,
        settings=settings,
        project=project,
        visibility=visibility,
        description=description,
    )


def ensure_repository(
    project: ProjectConfig,
    settings: Settings,
    *,
    push: bool,
    set_remote: bool = True,
    remote_protocol: str = "ssh",
    visibility: str = "private",
) -> None:
    resolve_forge(project).ensure_repository(
        project,
        settings,
        push=push,
        set_remote=set_remote,
        remote_protocol=remote_protocol,
        visibility=visibility,
    )


def create_issue(
    *,
    project: ProjectConfig,
    settings: Settings,
    title: str,
    body: str | None,
    labels: list[str],
) -> None:
    resolve_forge(project).create_issue(
        project=project, settings=settings, title=title, body=body, labels=labels
    )


def create_release(
    *,
    project: ProjectConfig,
    settings: Settings,
    tag: str,
    title: str | None,
    notes_file: Path | None = None,
    generate_notes: bool = True,
) -> None:
    resolve_forge(project).create_release(
        project=project,
        settings=settings,
        tag=tag,
        title=title,
        notes_file=notes_file,
        generate_notes=generate_notes,
    )


def build_repo_url(repo: str, protocol: str) -> str:
    # No `project` is available at this call site (pre-existing signature --
    # kept unchanged so every caller, e.g. `project_service.ensure_checkout`,
    # keeps working). Phase 1 only ever has one provider registered, so this
    # always resolves to the default GitHub provider.
    return FORGE_MAP["github"].build_repo_url(repo, protocol)
