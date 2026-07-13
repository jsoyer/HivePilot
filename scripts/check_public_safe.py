#!/usr/bin/env python3
"""
Public-safe guard: fails CI if org-specific content leaks into this public
repo's shipped example/default prompts.

Scans a configurable set of file globs (default: ``prompts/agents/*.md``,
relative to the repo root) against a denylist of regex patterns. Patterns
are read from ``scripts/public-denylist.txt`` and, optionally, from an
extra file pointed to by the ``PUBLIC_DENYLIST_EXTRA`` environment
variable. This lets maintainers keep sensitive (e.g. real personal name)
patterns in a private, uncommitted file while the structural patterns in
this repo stay public.

Usage:
    python scripts/check_public_safe.py [glob ...]

Exit codes:
    0 — no forbidden content found (or no denylist patterns at all).
    1 — forbidden content found; findings are printed to stdout.
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
DEFAULT_GLOBS = ["prompts/agents/*.md"]
EXTRA_DENYLIST_ENV_VAR = "PUBLIC_DENYLIST_EXTRA"


@dataclass(frozen=True)
class Finding:
    """A single forbidden-pattern match in a scanned file."""

    path: Path
    lineno: int
    text: str
    pattern: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.text} (pattern: {self.pattern})"


def parse_denylist_file(path: Path) -> list[str]:
    """Read raw pattern strings from a denylist file.

    Blank lines and lines starting with ``#`` are ignored. A missing file
    is treated as having no patterns (returns an empty list).
    """
    if not path.is_file():
        return []

    patterns: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile pattern strings into case-insensitive regexes."""
    return [re.compile(pattern) for pattern in patterns]


def load_all_patterns(
    denylist_path: Path = DEFAULT_DENYLIST_PATH,
) -> list[re.Pattern[str]]:
    """Load and compile patterns from the committed denylist plus the
    optional private extra denylist referenced by ``PUBLIC_DENYLIST_EXTRA``.
    """
    raw_patterns = parse_denylist_file(denylist_path)

    extra_path_str = os.environ.get(EXTRA_DENYLIST_ENV_VAR)
    if extra_path_str:
        extra_path = Path(extra_path_str)
        raw_patterns.extend(parse_denylist_file(extra_path))

    return compile_patterns(raw_patterns)


def scan_file(path: Path, patterns: list[re.Pattern[str]]) -> list[Finding]:
    """Scan a single file line-by-line for any denylist pattern match."""
    findings: list[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return findings

    for lineno, line in enumerate(lines, start=1):
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                findings.append(
                    Finding(
                        path=path,
                        lineno=lineno,
                        text=match.group(0),
                        pattern=pattern.pattern,
                    )
                )
    return findings


def expand_globs(globs: list[str]) -> list[Path]:
    """Expand a list of glob patterns (relative to REPO_ROOT, or absolute)
    into a sorted, de-duplicated list of existing file paths.
    """
    matched: set[Path] = set()
    for pattern in globs:
        candidates = (
            glob.glob(pattern) if os.path.isabs(pattern) else glob.glob(str(REPO_ROOT / pattern))
        )
        for candidate in candidates:
            candidate_path = Path(candidate)
            if candidate_path.is_file():
                matched.add(candidate_path)
    return sorted(matched)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    globs = args if args else DEFAULT_GLOBS

    patterns = load_all_patterns(DEFAULT_DENYLIST_PATH)
    files = expand_globs(globs)

    if not patterns:
        extra_set = bool(os.environ.get(EXTRA_DENYLIST_ENV_VAR))
        print(
            f"warning: no denylist patterns loaded (missing {DEFAULT_DENYLIST_PATH}"
            f"{' and ' + EXTRA_DENYLIST_ENV_VAR + ' file' if not extra_set else ''}); "
            "skipping public-safe check."
        )
        return 0

    all_findings: list[Finding] = []
    for file_path in files:
        all_findings.extend(scan_file(file_path, patterns))

    if all_findings:
        print("Public-safe check FAILED — org-specific content found:")
        for finding in all_findings:
            print(str(finding))
        return 1

    print(f"Public-safe check passed ({len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
