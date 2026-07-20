from __future__ import annotations

from pathlib import Path

import yaml
from git import Repo  # type: ignore

from hivepilot.config import settings
from hivepilot.models import GroupsFile, PipelinesFile, ProjectConfig, ProjectsFile, TasksFile
from hivepilot.services.github_service import build_repo_url
from hivepilot.utils.env import proxy_env
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_projects(path: Path | None = None) -> ProjectsFile:
    resolved = settings.resolve_config_path(path or settings.projects_file)
    return ProjectsFile.model_validate(_read_yaml(resolved))


def load_tasks(path: Path | None = None) -> TasksFile:
    resolved = settings.resolve_config_path(path or settings.tasks_file)
    return TasksFile.model_validate(_read_yaml(resolved))


def load_pipelines(path: Path | None = None) -> PipelinesFile:
    resolved = settings.resolve_config_path(path or settings.pipelines_file)
    return PipelinesFile.model_validate(_read_yaml(resolved))


def load_groups(path: Path | None = None) -> GroupsFile:
    resolved = settings.resolve_config_path(path or settings.groups_file)
    return GroupsFile.model_validate(_read_yaml(resolved))


def resolve_targets(name: str) -> list[str]:
    """Expand a group name to its component projects; a plain project name returns
    ``[name]``. Group lookup takes precedence over a same-named project."""
    groups = load_groups().groups
    if name in groups:
        return list(groups[name].components)
    return [name]


def ensure_checkout(project: ProjectConfig) -> None:
    """Clone ``project.path`` from ``project.owner_repo`` if the path doesn't exist yet.

    No-op if the path already exists -- byte-identical to pre-auto-clone
    behaviour for the common case. Fail-fast with a clear, actionable
    ``RuntimeError`` if the path is missing and there's no ``owner_repo`` to
    clone from, so a run surfaces a real error instead of a raw
    ``[Errno 2] No such file or directory`` deep inside a runner's
    ``subprocess.run(cwd=...)``.

    Every filesystem/clone operation (``Path.exists``, ``Path.mkdir``,
    ``Repo.clone_from``) is wrapped so ANY failure -- including a bare
    ``OSError``/``PermissionError`` from a read-only or non-writable
    filesystem, not just a git-specific error -- surfaces as a
    ``RuntimeError`` naming only the exception type, never the underlying
    exception's own message (which could embed a credential-bearing URL or
    an unrelated internal path). This is the single choke point the
    orchestrator's per-project preflight relies on: it only catches
    ``RuntimeError`` to isolate a failing project from the rest of the
    batch, so every failure mode here MUST end up as one.
    """
    try:
        exists = project.path.exists()
    except OSError as exc:
        raise RuntimeError(
            f"Failed to check project path {project.path}: {type(exc).__name__}"
        ) from exc
    if exists:
        return
    if not project.owner_repo:
        raise RuntimeError(
            f"Project path {project.path} does not exist and no 'owner_repo' is "
            f"configured to clone from. Set owner_repo: <owner>/<repo> in projects.yaml, "
            f"or clone the repo to {project.path} manually."
        )
    protocol = settings.project_clone_protocol
    url = build_repo_url(project.owner_repo, protocol)
    # Log the owner_repo slug + protocol, never the clone URL itself -- even
    # though build_repo_url never embeds credentials, this keeps the log
    # line safe by construction rather than by accident.
    logger.info(
        "project.autoclone",
        project=project.path.name,
        owner_repo=project.owner_repo,
        protocol=protocol,
    )
    try:
        project.path.parent.mkdir(parents=True, exist_ok=True)
        Repo.clone_from(url, str(project.path), env={**proxy_env()})
    except Exception as exc:  # noqa: BLE001 — wrap ANY mkdir/clone failure fail-fast
        raise RuntimeError(
            f"Failed to auto-clone {project.owner_repo} into {project.path}: {type(exc).__name__}"
        ) from exc
