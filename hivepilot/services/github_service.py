from __future__ import annotations

from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from hivepilot.config import Settings
from hivepilot.models import ProjectConfig
from hivepilot.utils.logging import get_logger
from hivepilot.utils.shell import run_command

logger = get_logger(__name__)


def repo_exists(slug: str, settings: Settings, project: ProjectConfig) -> bool:
    result = run_command([settings.gh_command, "repo", "view", slug], cwd=project.path, check=False, capture_output=True)
    return result.returncode == 0


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
def create_repo(slug: str, *, settings: Settings, project: ProjectConfig, visibility: str, description: str | None) -> None:
    args = [settings.gh_command, "repo", "create", slug, "--confirm"]
    if visibility == "private":
        args.append("--private")
    else:
        args.append("--public")
    if description:
        args.extend(["--description", description])
    run_command(args, cwd=project.path)


def ensure_repository(project: ProjectConfig, settings: Settings, push: bool) -> None:
    slug = project.owner_repo
    if not slug:
        raise ValueError("owner_repo missing in project configuration.")
    if repo_exists(slug, settings, project):
        logger.info("github.repo_exists", repo=slug)
    else:
        logger.info("github.repo_create", repo=slug)
        create_repo(slug, settings=settings, project=project, visibility="private", description=project.description)
    run_command(
        [settings.git_command, "remote", "set-url", "origin", build_repo_url(slug, "ssh")],
        cwd=project.path,
        check=False,
    )
    if push:
        run_command(
            [settings.git_command, "push", "-u", "origin", project.default_branch],
            cwd=project.path,
            check=False,
        )


def create_issue(
    *,
    project: ProjectConfig,
    settings: Settings,
    title: str,
    body: str | None,
    labels: list[str],
) -> None:
    slug = project.owner_repo
    if not slug:
        raise ValueError("owner_repo missing for issue creation")
    args = [
        settings.gh_command,
        "issue",
        "create",
        "--repo",
        slug,
        "--title",
        title,
    ]
    if body:
        args.extend(["--body", body])
    for label in labels:
        args.extend(["--label", label])
    run_command(args, cwd=project.path)


def create_release(
    *,
    project: ProjectConfig,
    settings: Settings,
    tag: str,
    title: str | None,
    notes_file: Path | None = None,
) -> None:
    slug = project.owner_repo
    if not slug:
        raise ValueError("owner_repo missing for release creation")
    args = [
        settings.gh_command,
        "release",
        "create",
        tag,
        "--repo",
        slug,
        "--generate-notes",
    ]
    if title:
        args.extend(["--title", title])
    if notes_file:
        args.extend(["--notes-file", str(notes_file)])
    run_command(args, cwd=project.path)


def build_repo_url(repo: str, protocol: str) -> str:
    if protocol == "https":
        return f"https://github.com/{repo}.git"
    return f"git@github.com:{repo}.git"
