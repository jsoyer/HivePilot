from __future__ import annotations

import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from git import GitCommandError, Repo  # type: ignore

from hivepilot.config import settings
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@contextmanager
def isolated_worktree(repo_path: Path, base_ref: str | None = None) -> Iterator[Path]:
    """Create a throwaway git worktree for `repo_path`, yield its Path, then remove it.

    The worktree is placed under `<repo_path>/.hivepilot-wt/<uuid>` (never under
    .claude/worktrees). On exit — even if the body raises — the worktree is removed
    with `git worktree remove --force`. Removal failures are logged as warnings and
    never re-raised, so cleanup never masks the original exception.

    Falls back to yielding `repo_path` itself when `git worktree add` fails (not a
    git repo, old git version, etc.) — the run continues in-place with a warning.
    """
    wt_base = repo_path / ".hivepilot-wt"
    wt_path = wt_base / str(uuid.uuid4())
    wt_path.mkdir(parents=True, exist_ok=True)
    git_ref = base_ref or "HEAD"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(wt_path), git_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "worktree.add_failed",
                repo=str(repo_path),
                error=result.stderr.strip(),
                fallback="in_place",
            )
            # Clean up the empty dir we created, fall back to real path
            try:
                wt_path.rmdir()
            except Exception:  # noqa: BLE001
                pass
            yield repo_path
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "worktree.add_exception", repo=str(repo_path), error=str(exc), fallback="in_place"
        )
        try:
            wt_path.rmdir()
        except Exception:  # noqa: BLE001
            pass
        yield repo_path
        return

    logger.info("worktree.created", path=str(wt_path), repo=str(repo_path))
    try:
        yield wt_path
    finally:
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            logger.info("worktree.removed", path=str(wt_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("worktree.remove_failed", path=str(wt_path), error=str(exc))


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


def commit_vault(
    vault_path: Path, message: str = "HivePilot: update Obsidian notes", *, push: bool = True
) -> bool:
    """git add/commit/push changes under the Obsidian *vault_path*.

    Best-effort and self-contained: returns False (no raise) if the vault is not a
    git work tree or has nothing to commit. Only the vault's own changes are staged.
    """
    try:
        repo = Repo(vault_path, search_parent_directories=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault.not_git_repo", path=str(vault_path), error=str(exc))
        return False
    # Scope every operation to the vault pathspec so we never stage/commit/push
    # unrelated changes that happen to be in the enclosing repo's index.
    pathspec = str(vault_path)
    repo.git.add("-A", "--", pathspec)
    if not repo.git.diff("--cached", "--name-only", "--", pathspec).strip():
        return False  # nothing changed under the vault
    repo.git.commit("-m", message, "--", pathspec)  # commit only the vault's paths
    if push:
        if repo.head.is_detached:
            logger.warning("vault.detached_head_no_push", path=pathspec)
        else:
            repo.git.push("origin", repo.active_branch.name)  # explicit remote + branch
    logger.info("vault.committed", path=pathspec, pushed=push)
    return True


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
