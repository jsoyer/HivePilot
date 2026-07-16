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


def test_promote_pr_builds_gh_ready_command(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.promote_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == "gh"
    assert cmd[1:3] == ["pr", "ready"]
    assert "hivepilot/x" in cmd
    assert m.call_args.kwargs["cwd"] == str(tmp_path)


def test_promote_pr_raises_on_gh_failure(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to promote PR"):
            git_service.promote_pr(project=project, branch="hivepilot/z", git=git)


def test_perform_git_actions_promotes_when_flag_set(tmp_path: Path) -> None:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    mock_promote.assert_called_once()


def test_perform_git_actions_promotes_before_merge(tmp_path: Path) -> None:
    """promote_pr must run before merge_pr when both flags are set (draft-then-promote,
    then merge, in one gate stage)."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True, merge_pr=True)
    calls: list[str] = []
    with (
        patch(
            "hivepilot.services.git_service.promote_pr",
            side_effect=lambda **_: calls.append("promote"),
        ),
        patch(
            "hivepilot.services.git_service.merge_pr", side_effect=lambda **_: calls.append("merge")
        ),
    ):
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    assert calls == ["promote", "merge"]


def test_perform_git_actions_skips_promote_when_verdict_blocked(tmp_path: Path) -> None:
    """CORRECTNESS: a gate stage whose own report parses to an explicit blocking
    verdict (BLOCK / BLOCKED / REQUEST_CHANGES / NEEDS_HUMAN / ...) must not
    promote the draft PR."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    blocked_report = "status: BLOCKED\nsummary:\n- found a critical issue\n"
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=blocked_report
        )
    mock_promote.assert_not_called()


def test_perform_git_actions_skips_merge_when_verdict_blocked(tmp_path: Path) -> None:
    """Same gate, applied to merge_pr for safety (a merge is even more final than
    promoting a draft)."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    blocked_report = "status: REQUEST_CHANGES\nsummary:\n- needs fixes\n"
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=blocked_report
        )
    mock_merge.assert_not_called()


@pytest.mark.parametrize(
    "verdict",
    ["BLOCK", "BLOCKED", "REQUEST_CHANGES", "NEEDS_HUMAN", "REJECTED", "FAILED", "DENIED"],
)
def test_perform_git_actions_skips_promote_on_each_blocking_verdict(
    tmp_path: Path, verdict: str
) -> None:
    """Every known blocking verdict (incl. NEEDS_HUMAN, which defers to a human)
    must skip promote — the PR stays a draft."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    report = f"status: {verdict}\nsummary:\n- see report\n"
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=report
        )
    mock_promote.assert_not_called()


@pytest.mark.parametrize("verdict", ["PASS", "APPROVE", "APPROVED", "CLEARED", "ADVISORY", "OK"])
def test_perform_git_actions_promotes_on_each_proceed_verdict(tmp_path: Path, verdict: str) -> None:
    """Heterogeneous approval vocabulary: the release gate approves with APPROVE,
    code roles with PASS, security with CLEARED, etc. — all must promote (a
    PASS-only whitelist would wrongly block the release gate on its own approval)."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    report = f"status: {verdict}\nsummary:\n- looks good\n"
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=report
        )
    mock_promote.assert_called_once()


def test_perform_git_actions_merges_when_verdict_approve(tmp_path: Path) -> None:
    """The release-gate approval verdict (APPROVE) must not be treated as blocked
    for merge either."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    approve_report = "status: APPROVE\nsummary:\n- ship it\n"
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=approve_report
        )
    mock_merge.assert_called_once()


def test_perform_git_actions_promotes_when_no_task_result(tmp_path: Path) -> None:
    """Legacy behaviour: no task_result (or unstructured text with no status:
    field) must NOT be treated as blocked, since most tasks aren't can_block
    roles and never emitted a structured report before this feature existed."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(project_name="p", project=project, git=ga, task_result=None)
    mock_promote.assert_called_once()

    with patch("hivepilot.services.git_service.promote_pr") as mock_promote2:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result="plain unstructured output"
        )
    mock_promote2.assert_called_once()
