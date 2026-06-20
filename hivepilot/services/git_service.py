from __future__ import annotations

import subprocess
from pathlib import Path

from git import GitCommandError, Repo  # type: ignore

from hivepilot.config import settings
from hivepilot.models import GitActions, ProjectConfig
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
    branch = f"{git.branch_prefix}/{project_name}"
    if git.commit or git.push:
        checkout_branch(project.path, branch)
        # The agent (e.g. claude) may have already committed its work; only commit
        # when there are uncommitted changes. The branch still carries the agent's
        # commits, so push/PR proceed either way.
        if git.commit and repo.is_dirty(untracked_files=True):
            repo.git.add("-A")
            message = git.commit_message or f"chore({project_name}): automated task run"
            repo.git.commit("-m", message)
        if git.push:
            push(project.path, "origin", branch)
    if git.create_pr:
        create_pr(project=project, branch=branch, git=git)
    if git.merge_pr:
        merge_pr(project=project, branch=branch, git=git)


def create_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Open a pull request via the gh CLI (run from the project repo)."""
    base = project.default_branch or "main"
    title = git.pr_title or f"HivePilot: {branch}"
    cmd = [settings.gh_command, "pr", "create", "--base", base, "--head", branch, "--title", title]
    if git.pr_body_file:
        cmd += ["--body-file", git.pr_body_file]
    else:
        cmd += ["--body", "Automated pull request opened by HivePilot."]
    try:
        subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
        logger.info("git.pr_created", project=project.path.name, branch=branch, base=base)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to create PR for {project.path.name}: {exc}") from exc


def merge_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Merge the open PR for *branch* via gh — Jules' autonomous final approval.

    Merge (not a review approval) because GitHub forbids approving your own PR, so
    the actionable autonomous step in a solo workflow is the merge itself.
    """
    method = git.merge_method if git.merge_method in {"merge", "squash", "rebase"} else "merge"
    cmd = [settings.gh_command, "pr", "merge", branch, f"--{method}"]
    try:
        subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
        logger.info("git.pr_merged", project=project.path.name, branch=branch, method=method)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to merge PR for {project.path.name}: {exc}") from exc
