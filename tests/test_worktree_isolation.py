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


# ---------------------------------------------------------------------------
# _execute_task `working_subdir` — first-class monorepo module scoping
# (monorepo-modules PRD). The exec cwd choke point (`_exec_project =
# project.model_copy(update={"path": _exec_path})`) is shared with worktree
# isolation above -- `working_subdir` (when set) further scopes `_exec_path`
# to `project.path / working_subdir` BEFORE that copy is made, so both
# features compose (a module target still gets an isolated worktree when
# `worktree_isolation` is on) instead of being mutually exclusive.
# ---------------------------------------------------------------------------


class TestExecuteTaskWorkingSubdir:
    """Duplicates `TestExecuteTaskWorktreeGating`'s small helpers (kept as a
    standalone class -- NOT a subclass -- so pytest doesn't re-collect/
    re-run the worktree-gating tests under a second class name; a plain
    attribute alias to the other class's methods confuses mypy's `self`
    binding, so these are copied rather than referenced)."""

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

    def test_working_subdir_scopes_exec_project_path(self, tmp_path: Path) -> None:
        """A `working_subdir` scopes the runner payload's effective cwd to
        `project.path / working_subdir` (worktree isolation off -- the
        common case for a plain, non-auto_git task run)."""
        repo = _init_git_repo(tmp_path / "repo")
        (repo / "apps" / "detection-core").mkdir(parents=True)
        project = self._make_project(repo)
        task = self._make_task(commit=False)

        captured_paths: list[Path] = []

        def _fake_capture_or_execute(runner_key, payload):
            captured_paths.append(payload.project.path)
            return "output"

        with (
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.perform_git_actions"),
            patch.object(
                __import__("hivepilot.orchestrator", fromlist=["Orchestrator"]).Orchestrator,
                "_capture_or_execute",
                side_effect=_fake_capture_or_execute,
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
                auto_git=False,
                simulate=False,
                working_subdir="apps/detection-core",
            )

        mock_wt.assert_not_called()
        assert captured_paths == [repo / "apps" / "detection-core"]

    def test_no_working_subdir_keeps_repo_root_cwd_byte_identical(self, tmp_path: Path) -> None:
        """`working_subdir=None` (the default) is byte-identical to before
        this feature existed -- the runner payload's cwd stays the repo
        root."""
        repo = _init_git_repo(tmp_path / "repo")
        project = self._make_project(repo)
        task = self._make_task(commit=False)

        captured_paths: list[Path] = []

        def _fake_capture_or_execute(runner_key, payload):
            captured_paths.append(payload.project.path)
            return "output"

        with (
            patch("hivepilot.orchestrator.settings") as mock_settings,
            patch("hivepilot.orchestrator.isolated_worktree") as mock_wt,
            patch("hivepilot.orchestrator.perform_git_actions"),
            patch.object(
                __import__("hivepilot.orchestrator", fromlist=["Orchestrator"]).Orchestrator,
                "_capture_or_execute",
                side_effect=_fake_capture_or_execute,
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
                auto_git=False,
                simulate=False,
            )

        mock_wt.assert_not_called()
        assert captured_paths == [repo]
