"""Agent file-ownership conflict DETECTION layer (Phase 16 C1).

This module is the standalone declaration + detection layer for multi-agent
file ownership: an optional `ownership.yaml` maps a role name to the glob
patterns of files it OWNS, and `detect_conflicts` reports when some OTHER
role changed a file owned by a role it isn't. It mirrors how `DebateConfig`
first shipped its config surface (declaration + a pure resolver) before the
orchestrator ever wired a gate on top of it.

The enforcement GATE — failing closed with a NEEDS_HUMAN verdict and actually
blocking a merge/stage progression on a detected conflict — is DEFERRED to a
follow-up sprint that wires this into `hivepilot.orchestrator` /
`hivepilot.services.git_service`. Those two modules are owned by a parallel
session for this sprint, so this module intentionally does not import or
modify either. `format_conflicts_as_needs_human` exists so that future
wiring sprint only has to call it — the message shape is decided here.

Fail-safe vs. fail-closed, deliberately different at each layer:

- `detect_conflicts` itself is a PURE function with a fail-SAFE default: no
  `ownership.yaml` (or a role that owns nothing) means no conflicts are
  reported. It just reports what it's given; it never blocks anything, since
  it has no gate to block yet.
- `load_file_ownership` is fail-CLOSED on a malformed (but present) config
  file: a bad `ownership.yaml` raises `ValueError` rather than silently
  falling back to "no ownership declared", the same convention used by
  `hivepilot.services.policy_service.get_policy`'s eager validation. An
  ABSENT file is treated as "ownership not opted into" (`{}`), not an error.
- The eventual GATE (not built here) is expected to treat any conflict
  `detect_conflicts` reports as fail-closed NEEDS_HUMAN, per the project's
  fail-closed-on-ambiguity convention (see `is_blocking` in
  `hivepilot.services.git_service` for the established pattern this will
  follow).

Anti-leak: `Conflict` and `format_conflicts_as_needs_human` carry only file
PATHS and ROLE NAMES — never file contents.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Conflict:
    """A file owned (by glob) by `owner_role` was changed by a different role."""

    path: str
    owner_role: str
    offending_role: str


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern (supporting `**`, `*`, `?`) to a compiled
    regex, anchored to a full match against a POSIX-style relative path.

    Portable across the repo's minimum supported Python (>=3.10) — no
    reliance on `pathlib.PurePath.full_match` (3.13+ only). `**` matches
    across directory separators (recursive); a single `*` matches within one
    path segment only (never crosses `/`); `?` matches exactly one character
    (never crosses `/`).

    A `**` immediately followed by `/` (e.g. `src/**/config.yaml`,
    `**/CODEOWNERS`) translates to `(?:.*/)?` — an OPTIONAL run of full path
    segments, each terminated by `/` — not a bare `.*`. A bare `.*` would
    only respect the boundary on the right of `**/` (via the fixed suffix
    that follows it), not the left, so e.g. `src/**/config.yaml` would wrongly
    reduce to `^src/.*config\\.yaml$` and match `src/myconfig.yaml` (a
    completely different file with no `/` boundary before `config.yaml`).
    `(?:.*/)?` only ever matches on a `/` boundary (or matches nothing at
    all), so `src/**/config.yaml` matches `src/config.yaml`,
    `src/a/config.yaml`, `src/a/b/config.yaml` -- never `src/myconfig.yaml`.
    A trailing `**` with no following `/` (e.g. `hivepilot/**`) keeps the
    original bare `.*` semantics (matches everything under `hivepilot/`,
    including nothing) since there's no right-hand segment to protect.
    """
    i = 0
    n = len(pattern)
    out: list[str] = []
    while i < n:
        char = pattern[i]
        if char == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    # "**/" -> zero-or-more FULL path segments, each ending
                    # in "/". Matching only ever lands on a "/" boundary, so
                    # this can never swallow a partial segment prefix.
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile(f"^{''.join(out)}$")


def _matches(pattern: str, path: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def detect_conflicts(
    ownership: Mapping[str, list[str]], changes: Mapping[str, Iterable[str]]
) -> list[Conflict]:
    """Report cross-role file-ownership conflicts.

    `ownership`: role name -> list of glob patterns that role OWNS.
    `changes`: role name -> the file paths that role modified.

    For every role R and every file F it changed, if F matches another role
    O's owned glob (O != R), emit `Conflict(path=F, owner_role=O,
    offending_role=R)`. A role changing a file it itself owns (even if
    another role's glob would also match, R == that role) is never flagged
    for that role's own ownership. Empty ownership (or a role that owns
    nothing) yields no conflicts — this function never raises and never
    blocks; it only reports.

    Returns a sorted (path, owner_role, offending_role), de-duplicated list —
    deterministic regardless of dict iteration order.
    """
    found: set[Conflict] = set()
    for offending_role, files in changes.items():
        for path in files:
            for owner_role, patterns in ownership.items():
                if owner_role == offending_role:
                    continue
                if any(_matches(pattern, path) for pattern in patterns):
                    found.add(
                        Conflict(path=path, owner_role=owner_role, offending_role=offending_role)
                    )
    return sorted(found, key=lambda c: (c.path, c.owner_role, c.offending_role))


def load_file_ownership(path: str | Path) -> dict[str, list[str]]:
    """Load an optional `ownership.yaml` (`{role: [globs]}`).

    Returns `{}` if the file is absent (ownership is opt-in). Raises
    `ValueError` with a clear message if the file exists but is malformed
    (not a dict of str -> list[str]) — fail-closed on bad config, mirroring
    `hivepilot.services.policy_service`'s eager validation convention.
    """
    resolved = Path(path)
    if not resolved.exists():
        return {}

    with resolved.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"Invalid ownership file {resolved}: top-level value must be a mapping of "
            f"role name -> list of glob patterns, got {type(raw).__name__}."
        )

    result: dict[str, list[str]] = {}
    for role, globs in raw.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError(
                f"Invalid ownership file {resolved}: role keys must be non-empty strings, "
                f"got {role!r}."
            )
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ValueError(
                f"Invalid ownership file {resolved}: role {role!r} must map to a list of "
                f"glob strings, got {globs!r}."
            )
        result[role] = list(globs)

    return result


def format_conflicts_as_needs_human(conflicts: list[Conflict]) -> str:
    """Render a NEEDS_HUMAN-style summary of `conflicts` for a future gate.

    Contains only file paths and role names — never file contents. Kept here
    so the eventual orchestrator/git_service wiring sprint only needs to call
    this, not decide the message shape itself.
    """
    if not conflicts:
        return "No file-ownership conflicts detected — clean."

    lines = [f"NEEDS_HUMAN: {len(conflicts)} file-ownership conflict(s) detected:"]
    for conflict in conflicts:
        lines.append(
            f"  - {conflict.path}: owned by '{conflict.owner_role}', "
            f"changed by '{conflict.offending_role}'"
        )
    return "\n".join(lines)
