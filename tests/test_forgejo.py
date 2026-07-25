"""Tests for hivepilot.forges.forgejo.ForgejoForge -- the self-hosted
Forgejo/Gitea ForgeProvider (Forge Phase 2). Mocks httpx at the module level
(no real network); asserts the exact method/URL/headers/body built for every
Forgejo REST call, fail-closed behaviour (missing forge_base_url/token,
non-https base_url), and that a resolved token is redacted everywhere."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.forges.forgejo import ForgejoForge
from hivepilot.forges.provider import FORGE_MAP
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services.config_provenance import (
    clear_secret_values,
    redact_text,
    registered_secret_values,
)


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    clear_secret_values()
    yield
    clear_secret_values()


@pytest.fixture()
def forge() -> ForgejoForge:
    return ForgejoForge()


def _project(tmp_path: Path, **kwargs: Any) -> ProjectConfig:
    defaults: dict[str, Any] = dict(
        path=tmp_path,
        forge="forgejo",
        forge_base_url="https://forge.example.com",
        owner_repo="acme/widgets",
        secrets={"FORGEJO_TOKEN": {"source": "env", "key": "FORGEJO_TOKEN_ENV"}},
    )
    defaults.update(kwargs)
    return ProjectConfig(**defaults)


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


def test_name_is_forgejo(forge: ForgejoForge) -> None:
    assert forge.name == "forgejo"


def test_registered_in_forge_map() -> None:
    assert "forgejo" in FORGE_MAP
    assert FORGE_MAP["forgejo"].name == "forgejo"


def test_resolve_forge_returns_forgejo_provider(tmp_path: Path) -> None:
    from hivepilot.forges.provider import resolve_forge

    project = _project(tmp_path)
    assert resolve_forge(project) is FORGE_MAP["forgejo"]


def test_build_repo_url_https(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert (
        forge.build_repo_url("acme/widgets", "https", project)
        == "https://forge.example.com/acme/widgets.git"
    )


def test_build_repo_url_ssh(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert (
        forge.build_repo_url("acme/widgets", "ssh", project)
        == "git@forge.example.com:acme/widgets.git"
    )


def test_build_repo_url_requires_project(forge: ForgejoForge) -> None:
    with pytest.raises(ValueError, match="requires project"):
        forge.build_repo_url("acme/widgets", "ssh")


def test_build_repo_url_fails_closed_without_base_url(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path, forge_base_url=None)
    with pytest.raises(ValueError, match="forge_base_url"):
        forge.build_repo_url("acme/widgets", "ssh", project)


def test_https_enforced_rejects_plaintext_http(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path, forge_base_url="http://forge.example.com")
    with pytest.raises(ValueError, match="Refusing plaintext"):
        forge.build_repo_url("acme/widgets", "https", project)


def test_https_enforcement_allows_loopback_http(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path, forge_base_url="http://localhost:3000")
    # Should not raise -- loopback http is the local-dev carve-out.
    url = forge.build_repo_url("acme/widgets", "https", project)
    assert url == "http://localhost:3000/acme/widgets.git"


def test_fails_closed_without_token_catalog_entry(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path, secrets={})
    with (
        patch("hivepilot.forges.forgejo.httpx") as mock_httpx,
        pytest.raises(Exception, match="FORGEJO_TOKEN"),
    ):
        forge.repo_exists("acme/widgets", settings, project)
    mock_httpx.request.assert_not_called()


def test_open_pr_builds_correct_request(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    git = GitActions(create_pr=True, pr_title="My PR")
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.open_pr(project=project, branch="hivepilot/x", git=git)
    call = mock_httpx.request.call_args
    method, url = call.args[0], call.args[1]
    assert method == "POST"
    assert url == "https://forge.example.com/api/v1/repos/acme/widgets/pulls"
    assert call.kwargs["headers"]["Authorization"] == "token sekret-token-value"
    body = call.kwargs["json"]
    assert body["title"] == "My PR"
    assert body["head"] == "hivepilot/x"
    assert body["base"] == "main"


def test_open_pr_draft_prefixes_wip(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    git = GitActions(create_pr=True, pr_title="My PR", draft=True)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.open_pr(project=project, branch="hivepilot/x", git=git)
    body = mock_httpx.request.call_args.kwargs["json"]
    assert body["title"] == "WIP: My PR"


def test_pr_status_resolves_index_then_fetches(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    list_resp = _mock_response(
        200,
        [{"number": 7, "head": {"ref": "hivepilot/x"}}, {"number": 8, "head": {"ref": "other"}}],
    )
    get_resp = _mock_response(200, {"state": "open", "merged": False})
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, get_resp]
        status = forge.pr_status(project=project, branch="hivepilot/x")
    assert status == "OPEN"
    second_call = mock_httpx.request.call_args_list[1]
    assert second_call.args[1] == "https://forge.example.com/api/v1/repos/acme/widgets/pulls/7"


def test_pr_status_no_matching_pr_raises(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(200, [])
        with pytest.raises(RuntimeError, match="No open PR"):
            forge.pr_status(project=project, branch="hivepilot/x")


def test_comment_pr_builds_correct_request(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    list_resp = _mock_response(200, [{"number": 3, "head": {"ref": "hivepilot/x"}}])
    comment_resp = _mock_response(201, {})
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, comment_resp]
        forge.comment_pr(project=project, branch="hivepilot/x", body="looks good")
    second_call = mock_httpx.request.call_args_list[1]
    assert second_call.args[0] == "POST"
    assert (
        second_call.args[1]
        == "https://forge.example.com/api/v1/repos/acme/widgets/issues/3/comments"
    )
    assert second_call.kwargs["json"] == {"body": "looks good"}


def test_merge_pr_builds_correct_request(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    git = GitActions(merge_pr=True, merge_method="squash")
    list_resp = _mock_response(200, [{"number": 5, "head": {"ref": "hivepilot/x"}}])
    merge_resp = _mock_response(200, {})
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, merge_resp]
        forge.merge_pr(project=project, branch="hivepilot/x", git=git)
    second_call = mock_httpx.request.call_args_list[1]
    assert second_call.args[0] == "POST"
    assert (
        second_call.args[1] == "https://forge.example.com/api/v1/repos/acme/widgets/pulls/5/merge"
    )
    assert second_call.kwargs["json"] == {"Do": "squash"}


def test_promote_pr_strips_wip_prefix(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    git = GitActions(promote_pr=True)
    list_resp = _mock_response(200, [{"number": 9, "head": {"ref": "hivepilot/x"}}])
    get_resp = _mock_response(200, {"title": "WIP: My PR"})
    patch_resp = _mock_response(200, {})
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.side_effect = [list_resp, get_resp, patch_resp]
        forge.promote_pr(project=project, branch="hivepilot/x", git=git)
    third_call = mock_httpx.request.call_args_list[2]
    assert third_call.args[0] == "PATCH"
    assert third_call.kwargs["json"] == {"title": "My PR"}


def test_repo_exists_true_on_200(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(200, {})
        assert forge.repo_exists("acme/widgets", settings, project) is True
    call = mock_httpx.request.call_args
    assert call.args[1] == "https://forge.example.com/api/v1/repos/acme/widgets"


def test_repo_exists_false_on_404(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(404, {})
        assert forge.repo_exists("acme/widgets", settings, project) is False


def test_create_issue_builds_correct_request(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.create_issue(
            project=project, settings=settings, title="bug", body="oops", labels=["a"]
        )
    call = mock_httpx.request.call_args
    assert call.args[1] == "https://forge.example.com/api/v1/repos/acme/widgets/issues"
    assert call.kwargs["json"] == {"title": "bug", "body": "oops", "labels": ["a"]}


def test_create_release_builds_correct_request(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(201, {})
        forge.create_release(project=project, settings=settings, tag="v1.0.0", title="Release")
    call = mock_httpx.request.call_args
    assert call.args[1] == "https://forge.example.com/api/v1/repos/acme/widgets/releases"
    assert call.kwargs["json"] == {"tag_name": "v1.0.0", "name": "Release"}


def test_token_never_appears_unredacted_in_raised_error(
    forge: ForgejoForge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN_ENV", "sekret-token-value")
    project = _project(tmp_path)
    with patch("hivepilot.forges.forgejo.httpx") as mock_httpx:
        mock_httpx.request.return_value = _mock_response(
            500, text="server exploded, token=sekret-token-value"
        )
        with pytest.raises(RuntimeError) as excinfo:
            forge.create_issue(project=project, settings=settings, title="x", body=None, labels=[])
    assert "sekret-token-value" not in str(excinfo.value)
    assert "sekret-token-value" in registered_secret_values()
    assert redact_text("sekret-token-value") != "sekret-token-value"


def test_ensure_repository_requires_owner_repo(forge: ForgejoForge, tmp_path: Path) -> None:
    project = _project(tmp_path, owner_repo=None)
    with pytest.raises(ValueError, match="owner_repo"):
        forge.ensure_repository(project, settings, push=False)
