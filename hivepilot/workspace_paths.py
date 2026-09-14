"""HP-116 workspace path confinement — relative, no ``..``, realpath stays in.

Coworker workspace-path pattern, rewritten in Python. This module does
**not** vendor TypeScript or Electron.

Every filesystem target an agent proposes goes through :func:`confine`
before it is opened or written:

- lexical path must be **relative** (no ``/``, ``~``, drive letter)
- ``..`` is refused even when it would stay inside the root after resolve
- ``os.path.realpath`` / symlink resolution must remain inside ``root``

HP-110 skill-evolution adds hidden-file / ``.`` rules on top of
:func:`normalize_relpath`. This module does not write files, create
schedules, or talk to PASS.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
PATH_ABSOLUTE = "path_absolute"
PATH_EMPTY = "path_empty"
PATH_NULL = "path_null"
PATH_TRAVERSAL = "path_traversal"
SYMLINK_ESCAPE = "symlink_escape"
PATH_NOT_STR = "path_not_str"

REFUSAL_CODES: tuple[str, ...] = (
    PATH_NOT_STR,
    PATH_EMPTY,
    PATH_NULL,
    PATH_ABSOLUTE,
    PATH_TRAVERSAL,
    SYMLINK_ESCAPE,
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class WorkspacePathError(ValueError):
    """Invalid confine arguments (not a path refusal)."""


@dataclass(frozen=True)
class ConfineResult:
    """Outcome of :func:`confine`. ``ok`` is the only success flag."""

    ok: bool
    root: Path
    rel: str = ""
    path: Path | None = None
    code: str = ""
    reason: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "root": str(self.root),
            "rel": self.rel,
            "path": None if self.path is None else str(self.path),
            "code": self.code,
            "reason": self.reason,
        }


def normalize_relpath(rel: object) -> tuple[str, str]:
    """Decode ``rel`` and return ``(cleaned, refusal_code)``.

    Empty ``refusal_code`` means the lexical form is a relative path
    with no ``..`` component. Hidden segments and ``.`` parts are left
    to callers (HP-110).
    """
    if not isinstance(rel, str):
        return "", PATH_NOT_STR
    if "\x00" in rel:
        return rel, PATH_NULL
    cleaned = unquote(rel.replace("\\", "/")).strip()
    if not cleaned:
        return "", PATH_EMPTY
    if cleaned.startswith("/") or cleaned.startswith("~"):
        return cleaned, PATH_ABSOLUTE
    if _DRIVE_RE.match(cleaned):
        return cleaned, PATH_ABSOLUTE
    path = Path(cleaned)
    if path.is_absolute():
        return cleaned, PATH_ABSOLUTE
    if any(part == ".." for part in path.parts):
        return cleaned, PATH_TRAVERSAL
    return cleaned, ""


def _refuse(root: Path, rel: str, code: str, reason: str) -> ConfineResult:
    return ConfineResult(ok=False, root=root, rel=rel, code=code, reason=reason)


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def confine(rel: object, *, root: Path | str) -> ConfineResult:
    """Resolve ``rel`` under ``root``. Refuse escape, ``..``, and absolutes.

    Both ``Path.resolve`` and ``os.path.realpath`` must stay inside the
    resolved root. A missing leaf is allowed when its existing prefix
    stays inside (``strict=False``).
    """
    if not isinstance(root, (Path, str)) or not str(root):
        raise WorkspacePathError("root must be a non-empty path")
    try:
        resolved_root = Path(root).resolve()
    except OSError as exc:
        raise WorkspacePathError(f"workspace root is unreadable: {exc}") from exc

    cleaned, code = normalize_relpath(rel)
    if code:
        reasons = {
            PATH_NOT_STR: "path must be a str",
            PATH_EMPTY: "path must be a non-empty relative",
            PATH_NULL: "path must not contain a NUL",
            PATH_ABSOLUTE: "path must be relative to the workspace",
            PATH_TRAVERSAL: "path must not contain '..'",
        }
        return _refuse(
            resolved_root,
            cleaned if isinstance(rel, str) else "",
            code,
            reasons.get(code, code),
        )

    candidate = resolved_root.joinpath(*Path(cleaned).parts)
    try:
        resolved = candidate.resolve(strict=False)
        real = Path(os.path.realpath(os.fspath(candidate)))
    except OSError as exc:
        return _refuse(
            resolved_root,
            cleaned,
            SYMLINK_ESCAPE,
            f"path could not be resolved: {exc}",
        )
    if not _inside(resolved, resolved_root) or not _inside(real, resolved_root):
        return _refuse(
            resolved_root,
            cleaned,
            SYMLINK_ESCAPE,
            "realpath/symlink escapes the workspace",
        )
    return ConfineResult(
        ok=True,
        root=resolved_root,
        rel=Path(cleaned).as_posix(),
        path=resolved,
    )


def confine_or_raise(rel: object, *, root: Path | str) -> Path:
    """Like :func:`confine` but raise :class:`WorkspacePathError` on refuse."""
    result = confine(rel, root=root)
    if not result.ok:
        raise WorkspacePathError(result.reason or result.code)
    if result.path is None:  # pragma: no cover - ok implies path
        raise WorkspacePathError("confine succeeded without a path")
    return result.path
