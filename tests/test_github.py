"""Tests for hivepilot.forges.github.GitHubForge -- the default ForgeProvider
(forge plugin type, Phase 1). Every method here is a direct port of the
pre-existing github_service.py/git_service.py gh-CLI logic, so these tests
mirror the shape of the old inline tests (mock run_command/subprocess.run,
assert the exact gh command built) -- proving the port is byte-identical."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.forges.github import GitHubForge
from hivepilot.models import GitActions, ProjectConfig


@pytest.fixture()
def forge() -> GitHubForge:
    return GitHubForge()


def test_name_is_github(forge: GitHubForge) -> None:
    assert forge.name == "github"


def test_build_repo_url_ssh_default(forge: GitHubForge) -> None:
    assert forge.build_repo_url("acme/widgets", "ssh") == "git@github.com:acme/widgets.git"


def test_build_repo_url_https(forge: GitHubForge) -> None:
    assert forge.build_repo_url("acme/widgets", "https") == "https://github.com/acme/widgets.git"


def test_repo_exists_true_on_zero_returncode(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.run_command") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert forge.repo_exists("acme/widgets", settings, project) is True
    cmd = mock_run.call_args.args[0]
    assert cmd == [settings.gh_command, "repo", "view", "acme/widgets"]


def test_repo_exists_false_on_nonzero_returncode(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.run_command") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        assert forge.repo_exists("acme/widgets", settings, project) is False


def test_create_repo_private_visibility(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.run_command") as mock_run:
        forge.create_repo(
            "acme/widgets",
            settings=settings,
            project=project,
            visibility="private",
            description="desc",
        )
    cmd = mock_run.call_args.args[0]
    assert "--private" in cmd
    assert "--description" in cmd and "desc" in cmd


def test_create_repo_public_visibility(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.run_command") as mock_run:
        forge.create_repo(
            "acme/widgets",
            settings=settings,
            project=project,
            visibility="public",
            description=None,
        )
    cmd = mock_run.call_args.args[0]
    assert "--public" in cmd
    assert "--description" not in cmd


def test_ensure_repository_creates_when_missing(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    with (
        patch.object(forge, "repo_exists", return_value=False) as mock_exists,
        patch.object(forge, "create_repo") as mock_create,
        patch("hivepilot.forges.github.run_command") as mock_run,
    ):
        forge.ensure_repository(project, settings, push=False)
    mock_exists.assert_called_once()
    mock_create.assert_called_once()
    # set_remote defaults True -> run_command called once for `git remote set-url`
    mock_run.assert_called_once()


def test_ensure_repository_skips_create_when_exists(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    with (
        patch.object(forge, "repo_exists", return_value=True),
        patch.object(forge, "create_repo") as mock_create,
        patch("hivepilot.forges.github.run_command"),
    ):
        forge.ensure_repository(project, settings, push=False)
    mock_create.assert_not_called()


def test_ensure_repository_pushes_when_flag_set(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets", default_branch="main")
    with (
        patch.object(forge, "repo_exists", return_value=True),
        patch("hivepilot.forges.github.run_command") as mock_run,
    ):
        forge.ensure_repository(project, settings, push=True, set_remote=False)
    push_cmd = mock_run.call_args.args[0]
    assert push_cmd == [settings.git_command, "push", "-u", "origin", "main"]


def test_ensure_repository_requires_owner_repo(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)  # no owner_repo
    with pytest.raises(ValueError, match="owner_repo"):
        forge.ensure_repository(project, settings, push=False)


def test_ensure_repository_rejects_bad_remote_protocol(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    with pytest.raises(ValueError, match="remote_protocol"):
        forge.ensure_repository(project, settings, push=False, remote_protocol="ftp")


def test_ensure_repository_rejects_bad_visibility(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    with pytest.raises(ValueError, match="visibility"):
        forge.ensure_repository(project, settings, push=False, visibility="secret")


def test_create_issue_builds_labels(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    with patch("hivepilot.forges.github.run_command") as mock_run:
        forge.create_issue(
            project=project, settings=settings, title="bug", body="oops", labels=["a", "b"]
        )
    cmd = mock_run.call_args.args[0]
    assert cmd.count("--label") == 2
    assert "a" in cmd and "b" in cmd


def test_create_issue_requires_owner_repo(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with pytest.raises(ValueError, match="owner_repo"):
        forge.create_issue(project=project, settings=settings, title="x", body=None, labels=[])


def test_create_release_generate_notes_default(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    with patch("hivepilot.forges.github.run_command") as mock_run:
        forge.create_release(project=project, settings=settings, tag="v1.0.0", title=None)
    cmd = mock_run.call_args.args[0]
    assert "--generate-notes" in cmd


def test_create_release_with_notes_file_skips_generate(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/widgets")
    notes = tmp_path / "notes.md"
    with patch("hivepilot.forges.github.run_command") as mock_run:
        forge.create_release(
            project=project, settings=settings, tag="v1.0.0", title="Release", notes_file=notes
        )
    cmd = mock_run.call_args.args[0]
    assert "--generate-notes" not in cmd
    assert "--notes-file" in cmd


def test_create_release_requires_owner_repo(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with pytest.raises(ValueError, match="owner_repo"):
        forge.create_release(project=project, settings=settings, tag="v1", title=None)


def test_open_pr_builds_gh_command(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path, default_branch="main")
    git = GitActions(create_pr=True, pr_title="My PR")
    with patch("hivepilot.forges.github.subprocess.run") as m:
        forge.open_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == settings.gh_command
    assert cmd[1:3] == ["pr", "create"]
    assert cmd[cmd.index("--title") + 1] == "My PR"


def test_open_pr_raises_on_failure(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(create_pr=True)
    with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to create PR"):
            forge.open_pr(project=project, branch="hivepilot/z", git=git)


def test_promote_pr_builds_gh_ready_command(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.forges.github.subprocess.run") as m:
        forge.promote_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[1:3] == ["pr", "ready"]


def test_promote_pr_raises_on_failure(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to promote PR"):
            forge.promote_pr(project=project, branch="hivepilot/z", git=git)


def test_merge_pr_respects_method(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True, merge_method="squash")
    with patch("hivepilot.forges.github.subprocess.run") as m:
        forge.merge_pr(project=project, branch="hivepilot/x", git=git)
    assert "--squash" in m.call_args.args[0]


def test_merge_pr_raises_on_failure(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True)
    with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to merge PR"):
            forge.merge_pr(project=project, branch="hivepilot/z", git=git)


def test_comment_pr_builds_gh_comment_command(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.subprocess.run") as m:
        forge.comment_pr(project=project, branch="hivepilot/x", body="looks good")
    cmd = m.call_args.args[0]
    assert cmd[1:3] == ["pr", "comment"]
    assert "hivepilot/x" in cmd
    assert cmd[cmd.index("--body") + 1] == "looks good"


def test_comment_pr_raises_on_failure(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to comment on PR"):
            forge.comment_pr(project=project, branch="hivepilot/z", body="x")


def test_pr_status_returns_stripped_stdout(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.subprocess.run") as m:
        m.return_value = MagicMock(stdout="OPEN\n")
        status = forge.pr_status(project=project, branch="hivepilot/x")
    assert status == "OPEN"
    cmd = m.call_args.args[0]
    assert cmd[1:3] == ["pr", "view"]


def test_pr_status_raises_on_failure(forge: GitHubForge, tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to fetch PR status"):
            forge.pr_status(project=project, branch="hivepilot/z")
