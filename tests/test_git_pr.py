"""Tests for git_service.create_pr (developer-opens-PR flow)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services import git_service


def test_create_pr_builds_gh_command(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, default_branch="main")
    git = GitActions(create_pr=True, pr_title="My PR")
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.create_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == "gh"
    assert cmd[1:3] == ["pr", "create"]
    assert cmd[cmd.index("--base") + 1] == "main"
    assert cmd[cmd.index("--head") + 1] == "hivepilot/x"
    assert cmd[cmd.index("--title") + 1] == "My PR"
    # run from the project repo
    assert m.call_args.kwargs["cwd"] == str(tmp_path)


def test_create_pr_default_title_and_body(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, default_branch="develop")
    git = GitActions(create_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.create_pr(project=project, branch="hivepilot/y", git=git)
    cmd = m.call_args.args[0]
    assert cmd[cmd.index("--base") + 1] == "develop"
    assert "--title" in cmd and "--body" in cmd  # defaults supplied


def test_create_pr_raises_on_gh_failure(tmp_path: Path) -> None:
    import pytest

    project = ProjectConfig(path=tmp_path)
    git = GitActions(create_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run", side_effect=OSError("gh boom")):
        with pytest.raises(RuntimeError, match="Failed to create PR"):
            git_service.create_pr(project=project, branch="hivepilot/z", git=git)
