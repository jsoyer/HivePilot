#!/usr/bin/env python3
"""
Public-safe guard: fails CI if org-specific content leaks into this PUBLIC
repo — shipped prompts, ENGINE CODE, config examples, docs and the web UI.

Originally this scanned ``prompts/agents/*.md`` only (9 files). That was the
narrow reading of "public-safe": prompts are the text handed verbatim to an
agent, so a customer's policy statement sitting there is the most direct leak.
But HivePilot is a generic orchestrator for ANY project and ANY organisation,
and a customer's document name baked into a module constant is exactly the
same leak with a longer fuse — it just ships as code instead of prose. The
prompts-only scope could not see it, which is how one customer's internal
document names survived in ``hivepilot/agent_rules.py`` long after the policy
statements around them were moved to config. The scan now covers engine code.

Scoped patterns
---------------
Not every pattern means the same thing everywhere. ``rtk proxy`` in a shipped
PROMPT is an instruction telling somebody else's agent to use the maintainer's
personal CLI wrapper — a leak. The same string in ``plugins/rtk.py`` is the
first-party, opt-in, PATH-gated plugin that *implements* that integration — a
feature. So denylist patterns carry a scope (``[any]`` / ``[prompts]``); see
``scripts/public-denylist.txt``. Widening the file set without scoping would
have forced the maintainer to delete a real pattern to unblock CI, which is
how guards get quietly hollowed out.

``tests/`` is deliberately NOT scanned
--------------------------------------
A guard's own regression tests must be able to spell out the strings they
forbid — ``tests/test_agent_rules_config.py::TestEngineDefaultIsTenantFree``
is a list of literal forbidden markers, and it is the single most valuable
test in this area. Scanning it would force that list to be obfuscated, making
the guard weaker to make the scanner pass. The exclusion is printed on every
successful run so it can never become an invisible hole.

Fail-closed
-----------
The failure mode this repository keeps re-shipping is an empty/absent value
read as "no constraint". A scan is a gate, so both of its inputs fail CLOSED:

* zero denylist patterns  -> exit 2 (NOT "passed"). A missing or emptied
  denylist means the guard has no teeth, not that the repo is clean.
* zero files matched, for ANY configured glob -> exit 2 (NOT "passed"). A
  renamed directory or a typo'd glob must not report success for having
  scanned nothing. This is the same defect as ``plugins audit`` reporting
  "No plugin source files found to audit" on a host with six plugins.

Usage:
    python scripts/check_public_safe.py [glob ...]

Exit codes:
    0 — files were scanned and no forbidden content was found.
    1 — forbidden content found; findings are printed to stdout.
    2 — the check could not run meaningfully (no patterns, or a glob that
        matched no files). Never silently reported as a pass.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DENYLIST_PATH = REPO_ROOT / "scripts" / "public-denylist.txt"
EXTRA_DENYLIST_ENV_VAR = "PUBLIC_DENYLIST_EXTRA"

#: Pattern scopes. ``any`` applies to every scanned file; ``prompts`` applies
#: only to files under ``prompts/`` (text shipped to agents verbatim).
SCOPE_ANY = "any"
SCOPE_PROMPTS = "prompts"
VALID_SCOPES = (SCOPE_ANY, SCOPE_PROMPTS)

#: Unscoped lines (and every line of a private ``PUBLIC_DENYLIST_EXTRA`` file)
#: default to the strictest scope, so a maintainer's private name patterns
#: apply everywhere without needing to know this file's syntax.
DEFAULT_SCOPE = SCOPE_ANY

#: Directory whose contents are "prompt text" for scope purposes.
PROMPTS_DIR = REPO_ROOT / "prompts"

#: Everything shipped in this public repo, minus ``tests/`` (see module docstring).
DEFAULT_GLOBS = [
    # Prompts handed to agents verbatim — the original scope.
    "prompts/**/*.md",
    # Engine code. The reason this file exists in its current form.
    "hivepilot/**/*.py",
    "plugins/**/*.py",
    "scripts/**/*.py",
    "workflows/**/*.py",
    # Shipped web UI.
    "web/src/**/*.ts",
    "web/src/**/*.tsx",
    # Shipped docs and config examples — a customer's policy leaks just as
    # easily through a YAML comment as through a prompt.
    "docs/**/*.md",
    "deploy/**/*.md",
    "examples/**/*.md",
    "examples/**/*.yaml",
    "*.md",
    "*.yaml",
    "*.yml",
]

#: Path (relative to REPO_ROOT) never scanned: the denylist enumerates the
#: forbidden strings by definition, so scanning it always "finds" all of them.
SELF_EXCLUDED = {DEFAULT_DENYLIST_PATH.resolve()}

#: Printed on every successful run so the carve-out stays visible.
NOT_SCANNED_NOTE = "tests/ excluded by design (a guard's tests must name what they forbid)"


@dataclass(frozen=True)
class Finding:
    """A single forbidden-pattern match in a scanned file."""

    path: Path
    lineno: int
    text: str
    pattern: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.text} (pattern: {self.pattern})"


@dataclass(frozen=True)
class ScopedPattern:
    """A compiled denylist pattern plus the file scope it applies to."""

    scope: str
    regex: re.Pattern[str]

    def applies_to(self, path: Path) -> bool:
        if self.scope == SCOPE_ANY:
            return True
        if self.scope == SCOPE_PROMPTS:
            return _is_under(path, PROMPTS_DIR)
        # Unreachable: parse_denylist_file rejects unknown scopes outright.
        return True


class DenylistError(RuntimeError):
    """A denylist file is syntactically unusable (bad scope or bad regex).

    Raised rather than skipped: a pattern that cannot be compiled is a pattern
    that silently protects nothing, which is precisely the failure this guard
    exists to prevent.
    """


def _is_under(path: Path, parent: Path) -> bool:
    """True if *path* is inside *parent* (both resolved)."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def strip_inline_comment(line: str) -> str:
    """Remove a trailing ``  # explanation`` from a denylist line.

    Only a ``#`` preceded by whitespace starts an inline comment, so a regex
    may still contain a literal ``#`` (e.g. ``issue#\\d+``).

    This function is the fix for a real, silent defect: ``parse_denylist_file``
    used to strip only WHOLE-line comments, so an entry written as::

        SOME-MARKER          # why this marker is forbidden

    compiled into a regex requiring that entire run of spaces and the comment
    text to appear in the scanned file. It therefore matched nothing, anywhere,
    ever. Exactly one entry in ``public-denylist.txt`` was written that way —
    the one naming a customer's internal document, i.e. the most important
    pattern in the file — and the check reported "passed" because of it. Same
    defect class as an empty denylist reading as "no constraint", just spelled
    differently.
    """
    match = re.search(r"\s+#", line)
    return line[: match.start()] if match else line


def parse_denylist_file(path: Path, default_scope: str = DEFAULT_SCOPE) -> list[tuple[str, str]]:
    """Read ``(scope, raw_pattern)`` pairs from a denylist file.

    Blank lines and whole-line ``#`` comments are ignored; trailing inline
    comments are stripped. ``[scope]`` section headers switch the scope applied
    to subsequent lines. A missing file is treated as having no patterns
    (returns an empty list) — callers decide whether that is fatal.

    Raises:
        DenylistError: on an unknown ``[scope]`` header.
    """
    if not path.is_file():
        return []

    entries: list[tuple[str, str]] = []
    scope = default_scope
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("[") and line.endswith("]"):
            candidate = line[1:-1].strip().lower()
            if candidate not in VALID_SCOPES:
                raise DenylistError(
                    f"{path}:{lineno}: unknown scope [{candidate}]; "
                    f"valid scopes are {', '.join(VALID_SCOPES)}"
                )
            scope = candidate
            continue

        pattern = strip_inline_comment(line).strip()
        if not pattern:
            continue
        entries.append((scope, pattern))
    return entries


def compile_patterns(
    patterns: list[str] | list[tuple[str, str]],
    default_scope: str = DEFAULT_SCOPE,
) -> list[ScopedPattern]:
    """Compile raw pattern strings (or ``(scope, pattern)`` pairs) into
    :class:`ScopedPattern` objects.

    Patterns are compiled case-SENSITIVELY: structural patterns such as
    ``{OBSIDIAN_VAULT}/[A-Z][A-Za-z]+/`` rely on case to avoid matching generic
    lowercase path segments.

    Raises:
        DenylistError: if a pattern is not a valid regular expression. An
            uncompilable pattern must never be silently dropped — that would
            leave the maintainer believing a marker is guarded when it is not.
    """
    compiled: list[ScopedPattern] = []
    for entry in patterns:
        scope, raw = entry if isinstance(entry, tuple) else (default_scope, entry)
        try:
            compiled.append(ScopedPattern(scope=scope, regex=re.compile(raw)))
        except re.error as exc:
            raise DenylistError(f"invalid denylist regex {raw!r}: {exc}") from exc
    return compiled


def load_all_patterns(
    denylist_path: Path = DEFAULT_DENYLIST_PATH,
) -> list[ScopedPattern]:
    """Load and compile patterns from the committed denylist plus the optional
    private extra denylist referenced by ``PUBLIC_DENYLIST_EXTRA``.

    Every entry of the private extra file gets ``SCOPE_ANY`` regardless of any
    section headers it contains: a private pattern is there because the string
    must not appear ANYWHERE, and silently narrowing it would be a fail-open.
    """
    entries = parse_denylist_file(denylist_path)

    extra_path_str = os.environ.get(EXTRA_DENYLIST_ENV_VAR)
    if extra_path_str:
        extra_entries = parse_denylist_file(Path(extra_path_str), default_scope=SCOPE_ANY)
        entries.extend((SCOPE_ANY, raw) for _scope, raw in extra_entries)

    return compile_patterns(entries)


def scan_file(path: Path, patterns: list[ScopedPattern]) -> list[Finding]:
    """Scan a single file line-by-line for any denylist pattern in scope."""
    findings: list[Finding] = []
    applicable = [p for p in patterns if p.applies_to(path)]
    if not applicable:
        return findings

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return findings

    for lineno, line in enumerate(lines, start=1):
        for pattern in applicable:
            match = pattern.regex.search(line)
            if match:
                findings.append(
                    Finding(
                        path=path,
                        lineno=lineno,
                        text=match.group(0),
                        pattern=pattern.regex.pattern,
                    )
                )
    return findings


def expand_glob(pattern: str) -> list[Path]:
    """Expand ONE glob (relative to REPO_ROOT, or absolute) into existing files."""
    target = pattern if os.path.isabs(pattern) else str(REPO_ROOT / pattern)
    raw = glob.glob(target, recursive=True)

    matched: list[Path] = []
    for candidate in raw:
        candidate_path = Path(candidate)
        if candidate_path.is_file() and candidate_path.resolve() not in SELF_EXCLUDED:
            matched.append(candidate_path)
    return matched


def expand_globs(globs: list[str]) -> tuple[list[Path], list[str]]:
    """Expand every glob.

    Returns ``(files, empty_globs)`` where *files* is sorted and de-duplicated
    and *empty_globs* lists the patterns that matched nothing. Reporting the
    empty ones separately is what lets ``main`` fail closed on a renamed
    directory instead of "passing" a scan of zero files.
    """
    matched: set[Path] = set()
    empty: list[str] = []
    for pattern in globs:
        found = expand_glob(pattern)
        if not found:
            empty.append(pattern)
        matched.update(found)
    return sorted(matched), empty


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    globs = args if args else list(DEFAULT_GLOBS)

    try:
        patterns = load_all_patterns(DEFAULT_DENYLIST_PATH)
    except DenylistError as exc:
        print(f"Public-safe check ERROR — unusable denylist: {exc}")
        return 2

    if not patterns:
        print(
            "Public-safe check ERROR — no denylist patterns loaded "
            f"(looked in {DEFAULT_DENYLIST_PATH}"
            f"{' and $' + EXTRA_DENYLIST_ENV_VAR if os.environ.get(EXTRA_DENYLIST_ENV_VAR) else ''}). "
            "A guard with no patterns protects nothing; refusing to report a pass."
        )
        return 2

    files, empty_globs = expand_globs(globs)

    if empty_globs:
        print("Public-safe check ERROR — these globs matched no files:")
        for pattern in empty_globs:
            print(f"  {pattern}")
        print(
            "A scan that silently covers nothing is not a passing scan. Fix the "
            "glob (or the renamed/removed directory) and re-run."
        )
        return 2

    if not files:
        print("Public-safe check ERROR — no files matched; refusing to report a pass.")
        return 2

    all_findings: list[Finding] = []
    for file_path in files:
        all_findings.extend(scan_file(file_path, patterns))

    if all_findings:
        print("Public-safe check FAILED — org-specific content found:")
        for finding in all_findings:
            print(str(finding))
        return 1

    print(
        f"Public-safe check passed ({len(files)} files scanned, "
        f"{len(patterns)} patterns). {NOT_SCANNED_NOTE}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
