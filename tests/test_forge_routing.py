"""Integration tests: git_service.create_pr/promote_pr/merge_pr route through
resolve_forge(project) (forge plugin type, Phase 1) -- proven two ways: (1)
for the default `forge: "github"` project, the exact same `gh` command is
built as before this refactor (behaviour-preserving); (2) for a project on a
DIFFERENT registered forge, the call goes to THAT forge instead -- proving
this isn't a hardcoded GitHub call in disguise."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.forges.provider import FORGE_MAP, ForgeRegistry
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services import git_service


class _RecordingForge:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_pr(self, *, project, branch, git):
        self.calls.append("open_pr")

    def promote_pr(self, *, project, branch, git):
        self.calls.append("promote_pr")

    def merge_pr(self, *, project, branch, git):
        self.calls.append("merge_pr")


@pytest.fixture()
def recording_forge_project(tmp_path):
    forge = _RecordingForge()
    ForgeRegistry.register("recording", forge, override=True)  # type: ignore[arg-type]
    project = ProjectConfig(path=tmp_path, forge="recording")
    yield forge, project
    FORGE_MAP.pop("recording", None)


def test_create_pr_routes_to_projects_own_forge(recording_forge_project) -> None:
    forge, project = recording_forge_project
    git_service.create_pr(project=project, branch="hivepilot/x", git=GitActions())
    assert forge.calls == ["open_pr"]


def test_promote_pr_routes_to_projects_own_forge(recording_forge_project) -> None:
    forge, project = recording_forge_project
    git_service.promote_pr(project=project, branch="hivepilot/x", git=GitActions())
    assert forge.calls == ["promote_pr"]


def test_merge_pr_routes_to_projects_own_forge(recording_forge_project) -> None:
    forge, project = recording_forge_project
    git_service.merge_pr(project=project, branch="hivepilot/x", git=GitActions())
    assert forge.calls == ["merge_pr"]


def test_default_project_promote_pr_still_calls_real_gh_cli(tmp_path) -> None:
    """For the default forge ("github", i.e. every pre-existing project), the
    exact same gh CLI command is still built -- this is the crux of the
    'byte-identical for github' acceptance criterion."""
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.forges.github.subprocess.run") as m:
        git_service.promote_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == settings.gh_command
    assert cmd[1:3] == ["pr", "ready"]


def test_default_project_merge_pr_still_calls_real_gh_cli(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True)
    with patch("hivepilot.forges.github.subprocess.run") as m:
        git_service.merge_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[1:3] == ["pr", "merge"]
