"""HP-116: relative-only workspace confine; ``..`` and symlink escape refuse."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.workspace_paths import (
    PATH_ABSOLUTE,
    PATH_EMPTY,
    PATH_NULL,
    PATH_TRAVERSAL,
    SYMLINK_ESCAPE,
    WorkspacePathError,
    confine,
    confine_or_raise,
    normalize_relpath,
)


def test_relative_path_stays_inside(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("ok", encoding="utf-8")
    result = confine("docs/readme.md", root=tmp_path)
    assert result.ok is True
    assert result.path == (tmp_path / "docs" / "readme.md").resolve()
    assert result.rel == "docs/readme.md"


def test_missing_leaf_inside_root_is_allowed(tmp_path: Path) -> None:
    result = confine("new/file.txt", root=tmp_path)
    assert result.ok is True
    assert result.path is not None
    assert result.path.is_relative_to(tmp_path.resolve())


def test_dotdot_is_blocked_even_when_it_would_stay_inside(tmp_path: Path) -> None:
    result = confine("docs/../src/main.py", root=tmp_path)
    assert result.ok is False
    assert result.code == PATH_TRAVERSAL


def test_parent_escape_is_blocked(tmp_path: Path) -> None:
    result = confine("../../etc/passwd", root=tmp_path)
    assert result.ok is False
    assert result.code == PATH_TRAVERSAL


def test_url_encoded_dotdot_is_blocked(tmp_path: Path) -> None:
    result = confine("%2e%2e/secret", root=tmp_path)
    assert result.ok is False
    assert result.code == PATH_TRAVERSAL


def test_absolute_unix_is_blocked(tmp_path: Path) -> None:
    result = confine("/etc/passwd", root=tmp_path)
    assert result.ok is False
    assert result.code == PATH_ABSOLUTE


def test_home_and_windows_drive_are_blocked(tmp_path: Path) -> None:
    assert confine("~/.ssh/id_rsa", root=tmp_path).code == PATH_ABSOLUTE
    assert confine("C:\\Windows\\System32", root=tmp_path).code == PATH_ABSOLUTE


def test_empty_and_nul_are_blocked(tmp_path: Path) -> None:
    assert confine("", root=tmp_path).code == PATH_EMPTY
    assert confine("foo\x00bar", root=tmp_path).code == PATH_NULL


def test_non_str_path_is_blocked(tmp_path: Path) -> None:
    assert confine(None, root=tmp_path).code == "path_not_str"


def test_escaping_symlink_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "leak").symlink_to(outside)
    result = confine("leak", root=workspace)
    assert result.ok is False
    assert result.code == SYMLINK_ESCAPE


def test_internal_symlink_is_allowed(tmp_path: Path) -> None:
    target = tmp_path / "real" / "file.txt"
    target.parent.mkdir()
    target.write_text("inside", encoding="utf-8")
    (tmp_path / "alias").symlink_to(target)
    result = confine("alias", root=tmp_path)
    assert result.ok is True
    assert result.path == target.resolve()


def test_symlink_parent_escape_on_new_leaf(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    result = confine("escape/new.txt", root=workspace)
    assert result.ok is False
    assert result.code == SYMLINK_ESCAPE


def test_confine_or_raise(tmp_path: Path) -> None:
    path = confine_or_raise("ok.md", root=tmp_path)
    assert path.is_relative_to(tmp_path.resolve())
    with pytest.raises(WorkspacePathError):
        confine_or_raise("../etc/passwd", root=tmp_path)


def test_normalize_relpath_does_not_treat_hidden_as_traversal() -> None:
    cleaned, code = normalize_relpath(".env")
    assert cleaned == ".env"
    assert code == ""
