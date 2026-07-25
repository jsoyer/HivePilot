"""Tests for hivepilot.forges.gitlab.GitLabForge -- the GitLab ForgeProvider
(Forge Phase 2). Mocks httpx at the module level (no real network); asserts
the exact method/URL/headers/body built for every GitLab REST call
(PR-concept -> Merge Request mapping), fail-closed behaviour (missing
token; non-https base_url), and the public gitlab.com default when
forge_base_url is unset."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.forges.gitlab import GitLabForge
from hivepilot.forges.provider import FORGE_MAP
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services.config_provenance import clear_secret_values, registered_secret_values


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    clear_secret_values()
    yield
    clear_secret_values()


@pytest.fixture()
def forge() -> GitLabForge:
    return GitLabForge()


def _project(tmp_path: Path, **kwargs: Any) -> ProjectConfig:
    defaults: dict[str, Any] = dict(
        path=tmp_path,
        forge="gitlab",
        forge_base_url="https://gitlab.example.com",
        owner_repo="acme/widgets",
        secrets={"GITLAB_TOKEN": {"source": "env", "key": "GITLAB_TOKEN_ENV"}},
    )
    defaults.update(kwargs)
    return ProjectConfig(**defaults)


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


def test_name_is_gitlab(forge: GitLabForge) -> None:
    assert forge.name == "gitlab"


def test_registered_in_forge_map() -> None:
    assert "gitlab" in FORGE_MAP
    assert FORGE_MAP["gitlab"].name == "gitlab"


def test_resolve_forge_returns_gitlab_provider(tmp_path: Path) -> None:
    from hivepilot.forges.provider import resolve_forge

    project = _project(tmp_path)
    assert resolve_forge(project) is FORGE_MAP["gitlab"]


def test_build_repo_url_https_uses_configured_base_url(forge: GitLabForge, tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert (
        forge.build_repo_url("acme/widgets", "https", project)
        == "https://gitlab.example.com/acme/widgets.git"
    )


def test_build_repo_url_ssh(forge: GitLabForge, tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert (
        forge.build_repo_url("acme/widgets", "ssh", project)
        == "git@gitlab.example.com:acme/widgets.git"
    )


def test_build_repo_url_defaults_to_public_gitlab_when_base_url_unset(
    forge: GitLabForge, tmp_path: Path
) -> None:
    project = _project(tmp_path, forge_base_url=None)
    assert (
        forge.build_repo_url("acme/widgets", "https", project)
        == "https://gitlab.com/acme/widgets.git"
    )


def test_build_repo_url_requires_project(forge: GitLabForge) -> None:
    with pytest.raises(ValueError, match="requires project"):
        forge.build_repo_url("acme/widgets", "ssh")


def test_https_enforced_rejects_plaintext_http(forge: GitLabForge, tmp_path: Path) -> None:
    project = _project(tmp_path, forge_base_url="http://gitlab.example.com")
    with pytest.raises(ValueError, match="Refusing plaintext"):
        forge.build_repo_url("acme/widgets", "https", project)


def test_https_enforcement_allows_loopback_http(forge: GitLabForge, tmp_path: Path) -> None:
    project = _project(tmp_path, forge_base_url="http://localhost:8080")
    url = forge.build_repo_url("acme/widgets", "https", project)
    assert url == "http://localhost:8080/acme/widgets.git"


def test_fails_closed_without_token_catalog_entry(forge: GitLabForge, tmp_path: Path) -> None:
    project = _project(tmp_path, secrets={})
    with (
        patch("hivepilot.forges.gitlab.httpx") as mock_httpx,
        pytest.raises(Exception, match="GITLAB_TOKEN"),
    ):
        forge.repo_exists("acme/widgets", settings, project)
    mock_httpx.request.assert_not_called()


def test_open_pr_builds_merge_request(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    git = GitActions(create_pr=True, pr_title="My MR")
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.open_pr(project=project, branch="hivepilot/x", git=git)
    call = mock_httpx.request.call_args
    method, url = call.args[0], call.args[1]
    assert method == "POST"
    assert url == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/merge_requests"
    assert call.kwargs["headers"]["PRIVATE-TOKEN"] == "sekret-gitlab-token"
    body = call.kwargs["json"]
    assert body["title"] == "My MR"
    assert body["source_branch"] == "hivepilot/x"
    assert body["target_branch"] == "main"


def test_open_pr_draft_prefixes_draft_marker(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    git = GitActions(create_pr=True, pr_title="My MR", draft=True)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.open_pr(project=project, branch="hivepilot/x", git=git)
    body = mock_httpx.request.call_args.kwargs["json"]
    assert body["title"] == "Draft: My MR"


def test_pr_status_finds_mr_by_source_branch(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(200, [{"iid": 12, "state": "opened"}])
        status = forge.pr_status(project=project, branch="hivepilot/x")
    assert status == "OPENED"
    call = mock_httpx.request.call_args
    assert call.args[0] == "GET"
    assert (
        call.args[1] == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/merge_requests"
    )
    assert call.kwargs["params"]["source_branch"] == "hivepilot/x"


def test_pr_status_no_matching_mr_raises(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(200, [])
        with pytest.raises(RuntimeError, match="No open"):
            forge.pr_status(project=project, branch="hivepilot/x")


def test_comment_pr_posts_note(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    list_resp = _mock_response(200, [{"iid": 4, "state": "opened"}])
    note_resp = _mock_response(201, {})
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, note_resp]
        forge.comment_pr(project=project, branch="hivepilot/x", body="looks good")
    second_call = mock_httpx.request.call_args_list[1]
    assert second_call.args[0] == "POST"
    assert (
        second_call.args[1]
        == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/merge_requests/4/notes"
    )
    assert second_call.kwargs["json"] == {"body": "looks good"}


def test_merge_pr_uses_merge_endpoint(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    git = GitActions(merge_pr=True, merge_method="squash")
    list_resp = _mock_response(200, [{"iid": 6, "state": "opened"}])
    merge_resp = _mock_response(200, {})
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, merge_resp]
        forge.merge_pr(project=project, branch="hivepilot/x", git=git)
    second_call = mock_httpx.request.call_args_list[1]
    assert second_call.args[0] == "PUT"
    assert (
        second_call.args[1]
        == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/merge_requests/6/merge"
    )
    assert second_call.kwargs["json"] == {"squash": True}


def test_promote_pr_strips_draft_prefix(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    git = GitActions(promote_pr=True)
    list_resp = _mock_response(200, [{"iid": 9, "state": "opened"}])
    get_resp = _mock_response(200, {"title": "Draft: My MR"})
    put_resp = _mock_response(200, {})
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, get_resp, put_resp]
        forge.promote_pr(project=project, branch="hivepilot/x", git=git)
    third_call = mock_httpx.request.call_args_list[2]
    assert third_call.args[0] == "PUT"
    assert third_call.kwargs["json"] == {"title": "My MR"}


def test_repo_exists_true_on_200(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(200, {})
        assert forge.repo_exists("acme/widgets", settings, project) is True
    call = mock_httpx.request.call_args
    assert call.args[1] == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets"


def test_repo_exists_false_on_404(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(404, {})
        assert forge.repo_exists("acme/widgets", settings, project) is False


def test_create_issue_builds_correct_request(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.create_issue(
            project=project, settings=settings, title="bug", body="oops", labels=["a", "b"]
        )
    call = mock_httpx.request.call_args
    assert call.args[1] == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/issues"
    assert call.kwargs["json"] == {"title": "bug", "description": "oops", "labels": "a,b"}


def test_create_release_builds_correct_request(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.create_release(project=project, settings=settings, tag="v1.0.0", title="Release")
    call = mock_httpx.request.call_args
    assert call.args[1] == "https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/releases"
    assert call.kwargs["json"] == {"tag_name": "v1.0.0", "name": "Release"}


def test_token_never_appears_unredacted_in_raised_error(
    forge: GitLabForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN_ENV", "sekret-gitlab-token")
    project = _project(tmp_path)
    with patch("hivepilot.forges.gitlab.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(
            500, text="server exploded, token=sekret-gitlab-token"
        )
        with pytest.raises(RuntimeError) as excinfo:
            forge.create_issue(project=project, settings=settings, title="x", body=None, labels=[])
    assert "sekret-gitlab-token" not in str(excinfo.value)
    assert "sekret-gitlab-token" in registered_secret_values()


def test_ensure_repository_requires_owner_repo(forge: GitLabForge, tmp_path: Path) -> None:
    project = _project(tmp_path, owner_repo=None)
    with pytest.raises(ValueError, match="owner_repo"):
        forge.ensure_repository(project, settings, push=False)
