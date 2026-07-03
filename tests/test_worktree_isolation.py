"""Tests for git worktree isolation in HivePilot."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services.git_service import isolated_worktree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be added."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True
    )
    (tmp_path / "README.md").write_text("init")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True
    )
    return tmp_path


# ---------------------------------------------------------------------------
# isolated_worktree tests
# ---------------------------------------------------------------------------


class TestIsolatedWorktree:
    def test_creates_and_removes_worktree(self, tmp_path: Path) -> None:
        """Worktree dir is created on entry and removed on clean exit."""
        repo = _init_git_repo(tmp_path / "repo")
        captured: list[Path] = []

        with isolated_worktree(repo) as wt_path:
            captured.append(wt_path)
            assert wt_path != repo, "Should yield a worktree, not the real repo"
            assert wt_path.exists(), "Worktree path should exist inside context"
            # The worktree should be a git directory (has .git file)
            assert (wt_path / ".git").exists() or (wt_path / ".git").is_file()

        assert len(captured) == 1
        assert not captured[0].exists(), "Worktree should be cleaned up after exit"

    def test_removes_worktree_even_on_exception(self, tmp_path: Path) -> None:
        """Worktree is removed even when the body raises an exception."""
        repo = _init_git_repo(tmp_path / "repo")
        captured: list[Path] = []

        with pytest.raises(ValueError, match="intentional"):
            with isolated_worktree(repo) as wt_path:
                captured.append(wt_path)
                raise ValueError("intentional error")

        assert len(captured) == 1
        assert not captured[0].exists(), "Worktree should be cleaned up even after exception"

    def test_falls_back_to_real_path_when_not_git_repo(self, tmp_path: Path) -> None:
        """When git worktree add fails (not a git repo), falls back to real path."""
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()

        with isolated_worktree(non_repo) as wt_path:
            assert wt_path == non_repo, "Should fall back to the real path"

    def test_worktree_path_is_under_hivepilot_wt(self, tmp_path: Path) -> None:
        """Worktree is created under .hivepilot-wt/, not .claude/worktrees."""
        repo = _init_git_repo(tmp_path / "repo")

        with isolated_worktree(repo) as wt_path:
            assert ".hivepilot-wt" in str(wt_path), "Worktree should be under .hivepilot-wt/"
            assert ".claude" not in str(wt_path), "Worktree must not be under .claude/"


# ---------------------------------------------------------------------------
# _execute_task worktree gating tests
# ---------------------------------------------------------------------------


class TestExecuteTaskWorktreeGating:
    """Unit tests for the worktree isolation gate inside _execute_task."""

    def _make_project(self, path: Path):
        from hivepilot.models import ProjectConfig

        return ProjectConfig(path=path)

    def _make_task(self, *, commit: bool = True, push: bool = False):
        from hivepilot.models import GitActions, TaskConfig, TaskStep

        return TaskConfig(
            description="test task",
            steps=[TaskStep(name="step1", runner="shell")],
            git=GitActions(commit=commit, push=push),
        )

    def _make_orchestrator(self, tmp_path: Path):
        """Build a minimal Orchestrator with mocked load functions."""
        from hivepilot.orchestrator import Orchestrator

        with (
            patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
            patch(
                "hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})
            ),
            patch("hivepilot.orchestrator.load_pipelines", return_value=MagicMock(pipelines={})),
            patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
            patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        ):
            orch = Orchestrator()
        return orch

    def test_uses_worktree_path_when_isolation_enabled(self, tmp_path: Path) -> None:
        """When worktree_isolation=True and project is a git repo, isolated_worktree is called."""
        repo = _init_git_repo(tmp_path / "repo")
        project = self._make_project(repo)
        task = self._make_task(commit=True)

        fake_wt_path = tmp_path / "fake-worktree"
        fake_wt_path.mkdir()

        from contextlib import contextmanager

        @contextmanager
        def _fake_wt(repo_path, base_ref=None):
            yield fake_wt_path

        with (
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.perform_git_actions"),
            patch.object(
                __import__("hivepilot.orchestrator", fromlist=["Orchestrator"]).Orchestrator,
                "_capture_or_execute",
                return_value="output",
            ),
        ):
            mock_settings.worktree_isolation = True
            mock_settings.stage_cache_enabled = False
            mock_wt.side_effect = _fake_wt

            orch = self._make_orchestrator(tmp_path)
            orch._execute_task(
                project=project,
                task_name="test-task",
                task=task,
                extra_prompt=None,
                auto_git=True,
                simulate=False,
            )

        mock_wt.assert_called_once()

    def test_uses_real_path_when_isolation_disabled(self, tmp_path: Path) -> None:
        """When worktree_isolation=False, isolated_worktree is never called."""
        repo = _init_git_repo(tmp_path / "repo")
        project = self._make_project(repo)
        task = self._make_task(commit=True)

        with (
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.perform_git_actions"),
            patch.object(
                __import__("hivepilot.orchestrator", fromlist=["Orchestrator"]).Orchestrator,
                "_capture_or_execute",
                return_value="output",
            ),
        ):
            mock_settings.worktree_isolation = False
            mock_settings.stage_cache_enabled = False

            orch = self._make_orchestrator(tmp_path)
            orch._execute_task(
                project=project,
                task_name="test-task",
                task=task,
                extra_prompt=None,
                auto_git=True,
                simulate=False,
            )

        mock_wt.assert_not_called()
