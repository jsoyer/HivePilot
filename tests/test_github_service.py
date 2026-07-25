"""Tests for hivepilot.services.github_service -- thin wrappers around
`resolve_forge(project)` (forge plugin type, Phase 1). Each wrapper is
tested two ways: (1) delegation -- a fake, non-GitHub forge registered
under the project's forge name receives the exact same call, proving the
wrapper resolves PER-PROJECT rather than hardcoding GitHub; and (2)
behaviour-preservation -- routed through the REAL (default) GitHubForge,
the same `gh`/`git` command is built as before this refactor."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.forges.provider import FORGE_MAP, ForgeRegistry
from hivepilot.models import ProjectConfig
from hivepilot.services import github_service


class _FakeForge:
    """A non-GitHub forge stand-in."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def repo_exists(self, slug, settings, project):
        self.calls.append(("repo_exists", {"slug": slug}))
        return True

    def create_repo(self, slug, *, settings, project, visibility, description):
        self.calls.append(
            ("create_repo", {"slug": slug, "visibility": visibility, "description": description})
        )

    def ensure_repository(
        self,
        project,
        settings,
        *,
        push,
        set_remote=True,
        remote_protocol="ssh",
        visibility="private",
    ):
        self.calls.append(("ensure_repository", {"push": push, "visibility": visibility}))

    def create_issue(self, *, project, settings, title, body, labels):
        self.calls.append(("create_issue", {"title": title, "labels": labels}))

    def create_release(
        self, *, project, settings, tag, title, notes_file=None, generate_notes=True
    ):
        self.calls.append(("create_release", {"tag": tag, "title": title}))

    def build_repo_url(self, repo, protocol):
        return f"fake://{repo}"


@pytest.fixture()
def fake_forge_project(tmp_path):
    forge = _FakeForge()
    ForgeRegistry.register("fake", forge, override=True)
    project = ProjectConfig(path=tmp_path, forge="fake")
    yield forge, project
    FORGE_MAP.pop("fake", None)


def test_repo_exists_delegates_to_resolved_forge(fake_forge_project) -> None:
    forge, project = fake_forge_project
    assert github_service.repo_exists("acme/x", settings, project) is True
    assert forge.calls == [("repo_exists", {"slug": "acme/x"})]


def test_create_repo_delegates_to_resolved_forge(fake_forge_project) -> None:
    forge, project = fake_forge_project
    github_service.create_repo(
        "acme/x", settings=settings, project=project, visibility="private", description="d"
    )
    assert forge.calls == [
        ("create_repo", {"slug": "acme/x", "visibility": "private", "description": "d"})
    ]


def test_ensure_repository_delegates_to_resolved_forge(fake_forge_project) -> None:
    forge, project = fake_forge_project
    github_service.ensure_repository(project, settings, push=True, visibility="public")
    assert forge.calls == [("ensure_repository", {"push": True, "visibility": "public"})]


def test_create_issue_delegates_to_resolved_forge(fake_forge_project) -> None:
    forge, project = fake_forge_project
    github_service.create_issue(
        project=project, settings=settings, title="t", body="b", labels=["x"]
    )
    assert forge.calls == [("create_issue", {"title": "t", "labels": ["x"]})]


def test_create_release_delegates_to_resolved_forge(fake_forge_project) -> None:
    forge, project = fake_forge_project
    github_service.create_release(project=project, settings=settings, tag="v1", title="Release")
    assert forge.calls == [("create_release", {"tag": "v1", "title": "Release"})]


# ---------------------------------------------------------------------------
# Behaviour-preservation: routed through the REAL (default) GitHub provider,
# every wrapper still shells out to the exact same gh/git commands as the
# pre-refactor inline implementation.
# ---------------------------------------------------------------------------


def test_build_repo_url_resolves_default_github_provider() -> None:
    assert github_service.build_repo_url("acme/x", "ssh") == "git@github.com:acme/x.git"
    assert github_service.build_repo_url("acme/x", "https") == "https://github.com/acme/x.git"


def test_repo_exists_routes_through_real_github_provider(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)  # forge defaults to "github"
    with patch("hivepilot.forges.github.run_command") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert github_service.repo_exists("acme/x", settings, project) is True
    cmd = mock_run.call_args.args[0]
    assert cmd == [settings.gh_command, "repo", "view", "acme/x"]


def test_ensure_repository_routes_through_real_github_provider(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/x", default_branch="main")
    with patch("hivepilot.forges.github.run_command") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)  # repo_exists -> True
        github_service.ensure_repository(project, settings, push=False)
    # repo_exists + remote set-url = 2 run_command calls, no create_repo
    assert mock_run.call_count == 2
    remote_cmd = mock_run.call_args_list[1].args[0]
    assert remote_cmd[:3] == [settings.git_command, "remote", "set-url"]


def test_create_issue_routes_through_real_github_provider(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/x")
    with patch("hivepilot.forges.github.run_command") as mock_run:
        github_service.create_issue(
            project=project, settings=settings, title="bug", body=None, labels=[]
        )
    cmd = mock_run.call_args.args[0]
    assert cmd[:4] == [settings.gh_command, "issue", "create", "--repo"]


def test_create_release_routes_through_real_github_provider(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path, owner_repo="acme/x")
    with patch("hivepilot.forges.github.run_command") as mock_run:
        github_service.create_release(project=project, settings=settings, tag="v1.0.0", title=None)
    cmd = mock_run.call_args.args[0]
    assert cmd[:4] == [settings.gh_command, "release", "create", "v1.0.0"]
