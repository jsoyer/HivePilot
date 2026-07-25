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


def resolve_project_target(
    target: str, projects: ProjectsFile | None = None
) -> tuple[ProjectConfig, str | None]:
    """Resolve a single run target into ``(project, module_subdir)``.

    Supports first-class monorepo module targeting (monorepo-modules PRD):
    ``"noxys/detection-core"`` resolves to the ``noxys`` project with
    ``module_subdir="apps/detection-core"`` (looked up in
    ``project.modules``); a plain ``"noxys"`` (no ``"/"``) resolves to the
    ``noxys`` project with ``module_subdir=None`` — byte-identical to
    before this function existed.

    Resolution order: (1) an EXACT match against a project key wins
    outright — even if *target* happens to contain a ``"/"``, a literal
    project key match is authoritative and is never re-interpreted as
    ``project/module``; (2) else, if it contains a ``"/"``, split on the
    first one and resolve the module against ``project.modules``; (3)
    else, the plain name is an unknown project.

    Group expansion is NOT this function's concern — a group name is
    resolved separately, BEFORE this function is ever called on an
    individual (already group-expanded) target, via ``resolve_targets``.
    Since no group is ever keyed with a ``"/"`` in practice, a
    ``project/module`` target is never mistaken for a group.

    Raises ``ValueError`` with a clear, actionable message for: an unknown
    project, a project with no ``modules`` configured, or an unknown
    module name (lists the available ones) — mirroring the existing
    ``Orchestrator._project``/"Unknown project" error shape so callers can
    treat it identically.
    """
    projects_file = projects if projects is not None else load_projects()
    if target in projects_file.projects:
        return projects_file.projects[target], None
    if "/" in target:
        project_name, module_name = target.split("/", 1)
        if project_name not in projects_file.projects:
            raise ValueError(f"Unknown project: {project_name}")
        project = projects_file.projects[project_name]
        if not project.modules:
            raise ValueError(
                f"Project '{project_name}' has no modules configured "
                f"(target was '{target}'). Add a 'modules:' map to its "
                f"projects.yaml entry, or run against '{project_name}' directly."
            )
        if module_name not in project.modules:
            available = ", ".join(sorted(project.modules))
            raise ValueError(
                f"Unknown module '{module_name}' for project '{project_name}'. "
                f"Available modules: {available}"
            )
        return project, project.modules[module_name]
    raise ValueError(f"Unknown project: {target}")


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
