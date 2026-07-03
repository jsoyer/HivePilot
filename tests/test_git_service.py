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


def test_commit_vault_commits_and_pushes(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.git.diff.return_value = "Notes/plan.md"  # staged changes present
    fake.head.is_detached = False
    fake.active_branch.name = "main"
    monkeypatch.setattr(git_service, "Repo", lambda *a, **k: fake)
    assert git_service.commit_vault(tmp_path, "msg", push=True) is True
    # add/commit scoped to the vault pathspec; push explicit remote+branch
    fake.git.add.assert_called_with("-A", "--", str(tmp_path))
    fake.git.commit.assert_called_with("-m", "msg", "--", str(tmp_path))
    fake.git.push.assert_called_once_with("origin", "main")


def test_commit_vault_no_changes_returns_false(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.git.diff.return_value = ""  # nothing staged
    monkeypatch.setattr(git_service, "Repo", lambda *a, **k: fake)
    assert git_service.commit_vault(tmp_path, "m") is False
    fake.git.commit.assert_not_called()


def test_commit_vault_not_a_repo_returns_false(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise Exception("not a repo")

    monkeypatch.setattr(git_service, "Repo", boom)
    assert git_service.commit_vault(tmp_path, "m") is False


def test_perform_git_actions_merges_when_flag_set(tmp_path: Path) -> None:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    mock_merge.assert_called_once()
