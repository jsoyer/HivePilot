from __future__ import annotations

import re
from pathlib import Path

from hivepilot.models import GitActions, ProjectConfig
from hivepilot.shell import ensure_command_available, run_command


class GitHubAutomationError(RuntimeError):
    pass



def _must_succeed(result, action: str) -> None:
    if result.returncode != 0:
        raise GitHubAutomationError(f"Failed to {action}.")



def _git_has_changes(project_path: Path, git_command: str, dry_run: bool) -> bool:
    result = run_command(
        command=[git_command, "status", "--porcelain"],
        cwd=project_path,
        dry_run=dry_run,
        show_command=False,
    )
    return bool(result.stdout.strip()) or dry_run



def _sanitize_ref(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    return slug.strip("-") or "task"



def perform_git_actions(
    *,
    project_name: str,
    task_name: str,
    project: ProjectConfig,
    git: GitActions,
    git_command: str,
    gh_command: str,
    dry_run: bool,
) -> None:
    ensure_command_available(git_command)
    if git.create_pr:
        ensure_command_available(gh_command)

    project_slug = _sanitize_ref(project_name)
    task_slug = _sanitize_ref(task_name)
    branch_suffix = f"{project_slug}-{task_slug}"
    if git.branch_prefix:
        branch_name = f"{git.branch_prefix.rstrip('/')}/{branch_suffix}"
    else:
        branch_name = branch_suffix

    checkout = run_command(
        command=[git_command, "checkout", "-B", branch_name],
        cwd=project.path,
        dry_run=dry_run,
    )
    _must_succeed(checkout, "create or switch branch")

    if not _git_has_changes(project.path, git_command, dry_run):
        raise GitHubAutomationError("No changes detected. Nothing to commit.")

    add = run_command(
        command=[git_command, "add", "-A"],
        cwd=project.path,
        dry_run=dry_run,
    )
    _must_succeed(add, "stage changes")

    if git.commit:
        message = git.commit_message or f"chore({project_name}): run orchestrated task"
        commit = run_command(
            command=[git_command, "commit", "-m", message],
            cwd=project.path,
            dry_run=dry_run,
        )
        _must_succeed(commit, "commit changes")

    if git.push:
        push = run_command(
            command=[git_command, "push", "-u", "origin", branch_name, "--force-with-lease"],
            cwd=project.path,
            dry_run=dry_run,
        )
        _must_succeed(push, "push branch")

    if git.create_pr:
        body_file = git.pr_body_file or "PR_BODY.md"
        body_path = project.path / body_file
        if not dry_run and not body_path.exists():
            raise GitHubAutomationError(
                f"PR body file not found: {body_path}. Create it or disable create_pr."
            )
        title = git.pr_title or f"Automated changes for {project_name}"
        pr_command = [
            gh_command,
            "pr",
            "create",
            "--base",
            project.default_branch,
            "--title",
            title,
            "--body-file",
            str(body_path),
        ]
        if project.owner_repo:
            pr_command.extend(["--repo", project.owner_repo])
        pr = run_command(
            command=pr_command,
            cwd=project.path,
            dry_run=dry_run,
        )
        _must_succeed(pr, "create pull request")
