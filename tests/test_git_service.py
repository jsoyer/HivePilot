"""Tests for git_service.merge_pr (Jules' autonomous final PR approval/merge)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services import git_service


def test_merge_pr_builds_gh_command(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True)  # default method = merge
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.merge_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == "gh"
    assert cmd[1:3] == ["pr", "merge"]
    assert "hivepilot/x" in cmd
    assert "--merge" in cmd
    assert m.call_args.kwargs["cwd"] == str(tmp_path)


def test_merge_pr_respects_method(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True, merge_method="squash")
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.merge_pr(project=project, branch="hivepilot/x", git=git)
    assert "--squash" in m.call_args.args[0]


def test_merge_pr_raises_on_gh_failure(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to merge PR"):
            git_service.merge_pr(project=project, branch="hivepilot/z", git=git)


def test_perform_git_actions_merges_when_flag_set(tmp_path: Path) -> None:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    mock_merge.assert_called_once()
