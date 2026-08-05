"""Summarise the generated parts of a diff so the review budget buys code.

The review subject is capped at 200 000 characters. A lockfile regeneration
or a rebuilt bundle can spend all of it on content no reviewer reads, and
push the actual change past the cut.

The obvious fix -- drop those paths -- is the wrong one. A changed
`package-lock.json` is where dependency substitution hides, and a diff that
never mentions it tells the CISO the file was untouched. So the filename and
the size of the change always survive; only the body is withheld, and the
placeholder says so and says how to get it.

Small changes to the same files are shown in full: a two-line lockfile edit
is the targeted one worth reading, and treating it like a 4 000-line
regeneration would hide the interesting case behind the noisy one.
"""

from __future__ import annotations

import re

__all__ = ["summarise_noisy_files", "summarise_with_report"]

_SECTION_START = "diff --git "

#: Below this, the body is cheap enough that showing it beats explaining it --
#: and a small edit to a generated file is the suspicious kind.
_SUMMARY_THRESHOLD_LINES = 50

#: Machine-written lines are long. Averaged over 50+ changed lines this
#: separates a minified bundle from anything a person typed, whatever the
#: path -- which matters for generated assets no basename rule would catch.
_GENERATED_LINE_WIDTH = 200

_NOISY_BASENAMES = frozenset(
    {
        "bun.lockb",
        "cargo.lock",
        "composer.lock",
        "flake.lock",
        "gemfile.lock",
        "go.sum",
        "mix.lock",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "packages.lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pubspec.lock",
        "uv.lock",
        "yarn.lock",
    }
)

_NOISY_SEGMENTS = frozenset(
    {".venv", "__snapshots__", "dist", "node_modules", "third_party", "vendor"}
)

_NOISY_SUFFIXES = (".min.js", ".min.css", ".map", ".lock")

_PATH_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)\s*$")

_PLACEHOLDER = (
    "[{path} is a generated or vendored file: {count} changed lines omitted "
    "from this review to leave room for the code. The change to it is real -- "
    "if it could matter (a substituted dependency, an unexpected rebuild), "
    "ask for it with `rtk gh pr diff` and judge it directly.]"
)


def _is_noisy_path(path: str) -> bool:
    lowered = path.lower()
    parts = lowered.split("/")
    if parts[-1] in _NOISY_BASENAMES:
        return True
    if _NOISY_SEGMENTS.intersection(parts[:-1]):
        return True
    return lowered.endswith(_NOISY_SUFFIXES)


def _changed_lines(body: list[str]) -> list[str]:
    return [line for line in body if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))]


def _looks_generated(changed: list[str]) -> bool:
    if not changed:
        return False
    width = sum(len(line) for line in changed) / len(changed)
    return width >= _GENERATED_LINE_WIDTH


def _split_sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith(_SECTION_START) and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    return sections


def _summarise_section(section: list[str]) -> tuple[list[str], str | None]:
    """Return the section to keep, and the path summarised (if any)."""
    match = _PATH_RE.match(section[0])
    if match is None:
        return section, None

    path = match.group("b")
    changed = _changed_lines(section[1:])
    if len(changed) < _SUMMARY_THRESHOLD_LINES:
        return section, None
    if not (_is_noisy_path(path) or _looks_generated(changed)):
        return section, None

    return [section[0], _PLACEHOLDER.format(path=path, count=len(changed))], path


def summarise_with_report(diff: str) -> tuple[str, list[str]]:
    """Summarise generated files, reporting which paths were summarised.

    The caller needs the report: a prompt that still claims to hold the
    complete change after bodies were withheld repeats the very defect this
    module exists to avoid.
    """
    if _SECTION_START not in diff:
        return diff, []

    kept: list[str] = []
    omitted: list[str] = []
    for section in _split_sections(diff.split("\n")):
        replacement, path = _summarise_section(section)
        kept.extend(replacement)
        if path is not None:
            omitted.append(path)
    return "\n".join(kept), omitted


def summarise_noisy_files(diff: str) -> str:
    return summarise_with_report(diff)[0]
