"""Tests for `hivepilot.services.project_service.ensure_checkout`.

Auto-clone of a missing project repo (PR B): when a project's `path` doesn't
exist and it has an `owner_repo`, clone it from that repo before the run --
instead of failing with a raw `[Errno 2] No such file or directory` deep
inside a runner's `subprocess.run(cwd=...)`.

Covers:
- path exists -> no-op, `Repo.clone_from` never called (byte-identical
  common-case behaviour)
- path missing + `owner_repo` set -> clones via the same
  `github_service.build_repo_url` slug->URL logic used elsewhere, into
  `project.path`, with the parent directory created first
- path missing + no `owner_repo` -> fails fast with a clear, actionable
  `RuntimeError` (mentions `owner_repo` and the path)
- `project_clone_protocol` setting controls ssh vs https URL construction
- a `clone_from` failure is wrapped in a `RuntimeError` naming only the
  exception type -- never a raw URL or credential-bearing message
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig
from hivepilot.services import project_service


def _permission_error_mkdir(self: Path, *args: object, **kwargs: object) -> None:
    raise PermissionError("Permission denied: /internal/secret/mount-detail")


def test_ensure_checkout_path_exists_is_noop(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = ProjectConfig(path=tmp_path)
    fake_repo = MagicMock()
    monkeypatch.setattr(project_service, "Repo", fake_repo)

    project_service.ensure_checkout(project)

    fake_repo.clone_from.assert_not_called()


def test_ensure_checkout_clones_missing_path_with_owner_repo(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "project_clone_protocol", "ssh", raising=False)
    target = tmp_path / "nested" / "widgets"
    project = ProjectConfig(path=target, owner_repo="acme/widgets")
    fake_repo = MagicMock()
    monkeypatch.setattr(project_service, "Repo", fake_repo)

    project_service.ensure_checkout(project)

    fake_repo.clone_from.assert_called_once()
    call_args, call_kwargs = fake_repo.clone_from.call_args
    assert call_args[0] == "git@github.com:acme/widgets.git"
    assert call_args[1] == str(project.path)
    assert "env" in call_kwargs
    # Parent directory must exist before the clone attempt.
    assert target.parent.exists()


def test_ensure_checkout_missing_path_no_owner_repo_raises_clear_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "missing-project"
    project = ProjectConfig(path=target)
    fake_repo = MagicMock()
    monkeypatch.setattr(project_service, "Repo", fake_repo)

    with pytest.raises(RuntimeError) as exc_info:
        project_service.ensure_checkout(project)

    message = str(exc_info.value)
    assert "owner_repo" in message
    assert str(project.path) in message
    fake_repo.clone_from.assert_not_called()


def test_ensure_checkout_https_protocol_builds_https_url(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "project_clone_protocol", "https", raising=False)
    target = tmp_path / "widgets"
    project = ProjectConfig(path=target, owner_repo="acme/widgets")
    fake_repo = MagicMock()
    monkeypatch.setattr(project_service, "Repo", fake_repo)

    project_service.ensure_checkout(project)

    call_args, _ = fake_repo.clone_from.call_args
    assert call_args[0] == "https://github.com/acme/widgets.git"


def test_ensure_checkout_clone_failure_wraps_exception_type_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "widgets"
    project = ProjectConfig(path=target, owner_repo="acme/widgets")
    fake_repo = MagicMock()
    sensitive = "https://user:sekret-token@github.com/acme/widgets.git failed"
    fake_repo.clone_from.side_effect = RuntimeError(sensitive)
    monkeypatch.setattr(project_service, "Repo", fake_repo)

    with pytest.raises(RuntimeError) as exc_info:
        project_service.ensure_checkout(project)

    message = str(exc_info.value)
    assert "acme/widgets" in message
    assert str(project.path) in message
    assert "RuntimeError" in message
    # Anti-leak: never echo the underlying exception message/URL/credentials.
    assert sensitive not in message
    assert "sekret-token" not in message


def test_ensure_checkout_mkdir_permission_error_wraps_to_runtime_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-writable parent (PermissionError/OSError from `Path.mkdir`) must
    be normalized to a `RuntimeError` too -- not just git-specific
    failures -- so the orchestrator's single `except RuntimeError` isolates
    THIS project without ever letting a bare OSError escape and abort the
    whole batch. The underlying exception's own message (which could name an
    unrelated internal filesystem path) must never be echoed."""
    target = tmp_path / "nested" / "widgets"
    project = ProjectConfig(path=target, owner_repo="acme/widgets")
    fake_repo = MagicMock()
    monkeypatch.setattr(project_service, "Repo", fake_repo)
    monkeypatch.setattr(Path, "mkdir", _permission_error_mkdir)

    with pytest.raises(RuntimeError) as exc_info:
        project_service.ensure_checkout(project)

    message = str(exc_info.value)
    assert "PermissionError" in message
    assert "internal/secret/mount-detail" not in message
    fake_repo.clone_from.assert_not_called()
