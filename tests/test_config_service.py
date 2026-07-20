"""Tests for hivepilot.services.config_service.

Verifies CONFIG_FILES enumerates every file that `hivepilot config sync`
must pull from the external config repo, including roles/groups/model
profiles alongside the existing project/task/pipeline/policy/schedule files.

Also verifies `HIVEPILOT_CONFIG_TOKEN` transient-header authentication for
private https config repos: the token is injected as a per-invocation
`http.extraheader` via `GIT_CONFIG_*` env vars (never written to
`.git/config`, never embedded in the repo URL, never logged), is scoped to
https repos only (ssh/`git@`/local paths ignore it), and leaves the
no-token path byte-identical to before this feature existed.
"""

from __future__ import annotations

import base64
import logging
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from git import InvalidGitRepositoryError

from hivepilot.config import settings
from hivepilot.services import config_service


def test_config_files_includes_expected_entries() -> None:
    expected = {
        "projects.yaml",
        "tasks.yaml",
        "pipelines.yaml",
        "policies.yaml",
        "schedules.yaml",
        "roles.yaml",
        "groups.yaml",
        "model_profiles.yaml",
    }
    assert expected <= config_service.CONFIG_FILES


def test_config_files_has_no_duplicates() -> None:
    # CONFIG_FILES is a set, but assert element count matches the expected
    # distinct set size to guard against accidental near-duplicate strings.
    assert len(config_service.CONFIG_FILES) == len(set(config_service.CONFIG_FILES))


# ---------------------------------------------------------------------------
# _auth_git_env
# ---------------------------------------------------------------------------


def test_auth_git_env_no_token_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "config_token", None, raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    assert config_service._auth_git_env() == {}


def test_auth_git_env_https_with_token_returns_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "config_token", "sekret-tok", raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    env = config_service._auth_git_env()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    header_value = env["GIT_CONFIG_VALUE_0"]
    assert header_value.startswith("Authorization: Basic ")
    b64 = header_value.removeprefix("Authorization: Basic ")
    decoded = base64.b64decode(b64).decode()
    assert decoded == "x-access-token:sekret-tok"


@pytest.mark.parametrize(
    "repo_url",
    [
        "git@github.com:you/config.git",
        "ssh://git@github.com/you/config.git",
        "/local/path/to/config-repo",
    ],
)
def test_auth_git_env_non_https_ignores_token(
    monkeypatch: pytest.MonkeyPatch, repo_url: str
) -> None:
    monkeypatch.setattr(settings, "config_token", "sekret-tok", raising=False)
    monkeypatch.setattr(settings, "config_repo", repo_url, raising=False)
    assert config_service._auth_git_env() == {}


# ---------------------------------------------------------------------------
# _open_or_clone: env passed to clone_from + update_environment on repo
# ---------------------------------------------------------------------------


def test_open_or_clone_no_token_env_is_byte_identical_to_proxy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proves zero behavior change for the existing (no-token) code path."""
    dest = tmp_path / "config-repo"
    monkeypatch.setattr(settings, "config_token", None, raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    monkeypatch.setattr(settings, "config_branch", "main", raising=False)
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)

    captured: dict = {}
    fake_repo = MagicMock()

    def fake_clone_from(url, path, branch=None, env=None):
        captured["env"] = env
        return fake_repo

    monkeypatch.setattr(config_service.Repo, "clone_from", staticmethod(fake_clone_from))

    result = config_service._open_or_clone()

    assert captured["env"] == (config_service.proxy_env() or None)
    fake_repo.git.update_environment.assert_not_called()
    assert result is fake_repo


def test_open_or_clone_with_token_merges_auth_env_into_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dest = tmp_path / "config-repo"
    monkeypatch.setattr(settings, "config_token", "sekret-tok", raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    monkeypatch.setattr(settings, "config_branch", "main", raising=False)
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)

    captured: dict = {}
    fake_repo = MagicMock()

    def fake_clone_from(url, path, branch=None, env=None):
        captured["env"] = env
        return fake_repo

    monkeypatch.setattr(config_service.Repo, "clone_from", staticmethod(fake_clone_from))

    result = config_service._open_or_clone()

    assert captured["env"]["GIT_CONFIG_COUNT"] == "1"
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert (
        "sekret-tok"
        in base64.b64decode(
            captured["env"]["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
        ).decode()
    )
    fake_repo.git.update_environment.assert_called_once()
    called_kwargs = fake_repo.git.update_environment.call_args.kwargs
    assert called_kwargs["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert result is fake_repo


def test_open_or_clone_reuse_path_also_gets_update_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `dest.exists()` reuse-open path must also carry the auth header."""
    dest = tmp_path / "config-repo"
    dest.mkdir()
    monkeypatch.setattr(settings, "config_token", "sekret-tok", raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)

    fake_repo = MagicMock()
    monkeypatch.setattr(config_service, "Repo", MagicMock(return_value=fake_repo))

    result = config_service._open_or_clone()

    fake_repo.git.update_environment.assert_called_once()
    called_kwargs = fake_repo.git.update_environment.call_args.kwargs
    assert called_kwargs["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert result is fake_repo


def test_open_or_clone_partial_clone_is_removed_before_reclone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A leftover/partial dir (Repo() raises InvalidGitRepositoryError) must be
    removed before re-cloning, or clone_from fails with "destination path
    already exists and is not empty"."""
    dest = tmp_path / "config-repo"
    dest.mkdir()
    (dest / "partial-junk").write_text("leftover")
    monkeypatch.setattr(settings, "config_token", None, raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    monkeypatch.setattr(settings, "config_branch", "main", raising=False)
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)

    fake_repo = MagicMock()

    def fake_clone_from(url, path, branch=None, env=None):
        assert not dest.exists() or not any(dest.iterdir())
        return fake_repo

    class FakeRepo:
        def __new__(cls, *_a, **_kw):
            raise InvalidGitRepositoryError("not a repo")

        clone_from = staticmethod(fake_clone_from)

    monkeypatch.setattr(config_service, "Repo", FakeRepo)

    rmtree_calls: list = []
    real_rmtree = shutil.rmtree

    def spy_rmtree(path, *args, **kwargs):
        rmtree_calls.append(Path(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(config_service.shutil, "rmtree", spy_rmtree)

    result = config_service._open_or_clone()

    assert rmtree_calls and rmtree_calls[0] == dest
    assert result is fake_repo


# ---------------------------------------------------------------------------
# Anti-leak: token must never appear in logs
# ---------------------------------------------------------------------------


def test_token_never_logged_during_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dest = tmp_path / "config-repo"
    monkeypatch.setattr(settings, "config_token", "super-secret-token-value", raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    monkeypatch.setattr(settings, "config_branch", "main", raising=False)
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    fake_repo = MagicMock()
    fake_repo.working_dir = str(tmp_path / "config-repo")
    (tmp_path / "config-repo").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_service.Repo, "clone_from", staticmethod(lambda *a, **k: fake_repo))
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)

    with caplog.at_level(logging.DEBUG):
        config_service.sync()

    header_b64 = base64.b64encode(b"x-access-token:super-secret-token-value").decode()
    for record in caplog.records:
        message = record.getMessage()
        record_dict = str(record.__dict__)
        assert "super-secret-token-value" not in message
        assert "super-secret-token-value" not in record_dict
        # Defense-in-depth (FIX B): the base64-encoded Authorization header
        # form must ALSO never appear, not just the raw token -- in case the
        # header itself (rather than the bare token) leaked into some future
        # log line.
        assert header_b64 not in message
        assert header_b64 not in record_dict


# ---------------------------------------------------------------------------
# push() also carries the auth header
# ---------------------------------------------------------------------------


def test_push_applies_auth_env_via_update_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dest = tmp_path / "config-repo"
    dest.mkdir()
    monkeypatch.setattr(settings, "config_token", "sekret-tok", raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://github.com/you/config.git", raising=False)
    monkeypatch.setattr(settings, "config_branch", "main", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setattr(config_service, "_config_dir", lambda: dest, raising=False)

    fake_repo = MagicMock()
    fake_repo.working_dir = str(dest)
    fake_repo.is_dirty.return_value = False
    monkeypatch.setattr(config_service, "Repo", MagicMock(return_value=fake_repo))

    config_service.push()

    fake_repo.git.update_environment.assert_called_once()
    called_kwargs = fake_repo.git.update_environment.call_args.kwargs
    assert called_kwargs["GIT_CONFIG_KEY_0"] == "http.extraheader"
