from __future__ import annotations

import subprocess
from pathlib import Path

from git import Repo, GitCommandError  # type: ignore

from hivepilot.models import GitActions, ProjectConfig

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def ensure_repo(path: Path) -> Repo:
    try:
        return Repo(path)
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"{path} is not a git repository: {exc}") from exc


def checkout_branch(path: Path, branch: str) -> None:
    repo = ensure_repo(path)
    git = repo.git
    try:
        git.checkout("-B", branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to checkout {branch}: {exc}") from exc


def push(path: Path, remote: str, branch: str) -> None:
    repo = ensure_repo(path)
    try:
        repo.git.push("-u", remote, branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to push {branch}: {exc}") from exc


def status(path: Path) -> str:
    repo = ensure_repo(path)
    return repo.git.status("--short")


def run_git_command(args: list[str], cwd: Path) -> None:
    subprocess.run([settings.git_command, *args], cwd=str(cwd), check=True)


def perform_git_actions(
    *,
    project_name: str,
    project: ProjectConfig,
    git: GitActions,
) -> None:
    repo = ensure_repo(project.path)
    if not repo.is_dirty(untracked_files=True):
        raise RuntimeError(f"No changes detected for {project_name}")
    branch = f"{git.branch_prefix}/{project_name}"
    checkout_branch(project.path, branch)
    repo.git.add("-A")
    if git.commit:
        message = git.commit_message or f"chore({project_name}): automated task run"
        repo.git.commit("-m", message)
    if git.push:
        push(project.path, "origin", branch)
