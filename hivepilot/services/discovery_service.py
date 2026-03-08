from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from hivepilot.config import settings
from hivepilot.models import ProjectConfig
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def discover_local_projects(
    roots: Iterable[Path],
    *,
    include_hidden: bool = False,
    max_depth: int = 3,
) -> list[ProjectConfig]:
    """Scan directories for git repositories and return ProjectConfig entries."""
    projects: list[ProjectConfig] = []
    roots = [settings.resolve_path(root) for root in roots]
    for root in roots:
        if not root.exists():
            continue
        for current_root, dirnames, filenames in os.walk(root):
            depth = Path(current_root).relative_to(root).parts
            if len(depth) > max_depth:
                dirnames[:] = []
                continue
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if ".git" in dirnames:
                project_path = Path(current_root)
                repo_name = project_path.name
                projects.append(
                    ProjectConfig(
                        path=project_path,
                        description=f"Discovered project at {project_path}",
                    )
                )
                dirnames[:] = []
    logger.info("discovery.local", count=len(projects))
    return projects


def discover_github_repos(*, org: str, token_env: str = "GITHUB_TOKEN") -> list[ProjectConfig]:
    """Fetch repositories from GitHub organization using gh CLI."""
    token = os.environ.get(token_env)
    if not token:
        logger.warning("discovery.github.missing_token", env_var=token_env)
        return []
    import requests

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    url = f"https://api.github.com/orgs/{org}/repos?per_page=200"
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    repos = response.json()
    projects: list[ProjectConfig] = []
    for repo in repos:
        projects.append(
            ProjectConfig(
                path=Path(repo["name"]),  # placeholder – user must set actual path
                description=repo.get("description"),
                default_branch=repo.get("default_branch", "main"),
                owner_repo=repo["full_name"],
            )
        )
    logger.info("discovery.github", org=org, count=len(projects))
    return projects
