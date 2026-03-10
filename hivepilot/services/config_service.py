from __future__ import annotations

from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo  # type: ignore

from hivepilot.config import settings
from hivepilot.utils.env import proxy_env
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Files managed by config sync — anything else in base_dir is left alone
CONFIG_FILES = {
    "projects.yaml",
    "tasks.yaml",
    "pipelines.yaml",
    "policies.yaml",
    "schedules.yaml",
}
CONFIG_DIRS = {"prompts"}


def _require_config_repo() -> str:
    if not settings.config_repo:
        raise RuntimeError(
            "No config repo configured. Set HIVEPILOT_CONFIG_REPO=git@github.com:you/hivepilot-config"
        )
    return settings.config_repo


def _config_dir() -> Path:
    """Local directory where the config repo is cloned (~/.local/share/hivepilot/config-repo)."""
    return settings.xdg_data_home / "config-repo"


def _open_or_clone() -> Repo:
    """Return an open Repo, cloning from config_repo if it doesn't exist yet."""
    repo_url = _require_config_repo()
    dest = _config_dir()

    if dest.exists():
        try:
            return Repo(dest)
        except InvalidGitRepositoryError:
            pass  # fall through to re-clone

    logger.info("config.clone", repo=repo_url, dest=str(dest))
    env = {**proxy_env()}
    return Repo.clone_from(repo_url, str(dest), branch=settings.config_branch, env=env or None)


def sync() -> list[str]:
    """
    Pull latest config from remote and copy managed files into base_dir.
    Returns list of files that were updated.
    """
    repo = _open_or_clone()
    branch = settings.config_branch

    try:
        repo.git.fetch("origin")
        repo.git.checkout(branch)
        repo.git.reset("--hard", f"origin/{branch}")
        logger.info("config.sync.pulled", branch=branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to pull config: {exc}") from exc

    updated = _copy_to_base_dir(repo)
    logger.info("config.sync.done", updated=updated)
    return updated


def push(message: str = "chore: update config") -> None:
    """
    Copy managed files from base_dir into the config repo, commit and push.
    No-ops if there are no changes.
    """
    repo = _open_or_clone()
    branch = settings.config_branch

    try:
        repo.git.checkout(branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to checkout branch {branch}: {exc}") from exc

    _copy_from_base_dir(repo)

    if not repo.is_dirty(untracked_files=True):
        logger.info("config.push.no_changes")
        return

    repo.git.add("-A")
    repo.git.commit("-m", message)
    try:
        repo.git.push("origin", branch)
        logger.info("config.push.done", branch=branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to push config: {exc}") from exc


def get_status() -> str:
    """Return `git status --short` of the config repo clone."""
    try:
        repo = Repo(_config_dir())
    except (InvalidGitRepositoryError, Exception):
        return "(config repo not cloned — run `hivepilot config sync` first)"
    return repo.git.status("--short") or "(clean)"


def get_log(n: int = 10) -> str:
    """Return last n commits from the config repo."""
    try:
        repo = Repo(_config_dir())
    except (InvalidGitRepositoryError, Exception):
        return "(config repo not cloned)"
    return repo.git.log("--oneline", f"-{n}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _copy_to_base_dir(repo: Repo) -> list[str]:
    """Copy config files from clone → XDG config home. Returns list of copied paths."""
    src = Path(repo.working_dir)
    dst = settings.xdg_config_home
    dst.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []

    for name in CONFIG_FILES:
        src_file = src / name
        if src_file.exists():
            dst_file = dst / name
            new_content = src_file.read_bytes()
            if not dst_file.exists() or dst_file.read_bytes() != new_content:
                dst_file.write_bytes(new_content)
                updated.append(name)
                logger.debug("config.sync.file", file=name)

    for dir_name in CONFIG_DIRS:
        src_dir = src / dir_name
        if src_dir.exists():
            dst_dir = dst / dir_name
            for src_file in src_dir.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(src_dir)
                    dst_file = dst_dir / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    new_content = src_file.read_bytes()
                    if not dst_file.exists() or dst_file.read_bytes() != new_content:
                        dst_file.write_bytes(new_content)
                        updated.append(f"{dir_name}/{rel}")
                        logger.debug("config.sync.file", file=f"{dir_name}/{rel}")

    return updated


def _copy_from_base_dir(repo: Repo) -> None:
    """Copy managed config files from XDG config home → clone."""
    src = settings.xdg_config_home
    dst = Path(repo.working_dir)

    for name in CONFIG_FILES:
        src_file = src / name
        if src_file.exists():
            dst_file = dst / name
            dst_file.write_bytes(src_file.read_bytes())

    for dir_name in CONFIG_DIRS:
        src_dir = src / dir_name
        if src_dir.exists():
            dst_dir = dst / dir_name
            for src_file in src_dir.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(src_dir)
                    dst_file = dst_dir / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    dst_file.write_bytes(src_file.read_bytes())
