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

And nothing is withheld at all unless something survives to be read. A PR
made entirely of generated files is passed through whole -- see the note in
`summarise_with_report`.
"""

from __future__ import annotations

import re

__all__ = ["summarise_noisy_files", "summarise_with_report"]

_SECTION_START = "diff --git "

#: Below BOTH of these, the body is cheap enough that showing it beats
#: explaining it -- and a small edit to a generated file is the suspicious
#: kind, the one worth reading in full.
#:
#: Two measures because a lockfile and a bundle are big in different ways.
#: The first version gated on line count alone and measured 0% on the three
#: real PRs it was written for: git diffs a minified bundle as 2-4 lines of
#: 280 000 characters each, so it fell under a 50-line threshold and was
#: never even tested against the generated-content rules below.
_SUMMARY_MIN_LINES = 50
_SUMMARY_MIN_CHARS = 2_000

#: Machine-written lines are long -- long enough that this separates a
#: minified bundle from anything a person typed, whatever the path, which
#: matters for generated assets no basename rule would catch.
#:
#: Set from measurement, with room on both sides. The narrowest real bundle
#: line in this repo's own committed assets is ~44 000 characters (the CSS
#: bundle); the widest are 214 000 and 286 000 (PRs 400 and 413). Against
#: that, a long hand-written line -- a reflowed markdown paragraph, a dense
#: dict literal -- runs to maybe 1 000. An earlier 200 misclassified three
#: lines of ordinary prose as generated, which would have summarised a real
#: source file out of the review: a false positive here costs a blind spot,
#: so the gap is deliberately wide rather than tight.
_GENERATED_LINE_WIDTH = 5_000

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

#: States both measures, because either alone misreads one of the two shapes:
#: "4 changed lines" hides a 1.1 MB bundle rebuild, and a character count
#: alone hides a 4 000-line lockfile regeneration.
_PLACEHOLDER = (
    "[{path} is a generated or vendored file: {count} changed lines "
    "({chars} characters) omitted from this review to leave room for the "
    "code. The change to it is real -- if it could matter (a substituted "
    "dependency, an unexpected rebuild), ask for it with `rtk gh pr diff` "
    "and judge it directly.]"
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
    volume = sum(len(line) for line in changed)
    if len(changed) < _SUMMARY_MIN_LINES and volume < _SUMMARY_MIN_CHARS:
        return section, None
    if not (_is_noisy_path(path) or _looks_generated(changed)):
        return section, None

    placeholder = _PLACEHOLDER.format(
        path=path, count=len(changed), chars=f"{volume:,}".replace(",", " ")
    )
    return [section[0], placeholder], path


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
    file_sections = 0
    for section in _split_sections(diff.split("\n")):
        # Only a real `diff --git` section counts towards "is anything left
        # to read": a preamble is not content a reviewer can judge, and
        # counting it would quietly defeat the all-noise check below.
        if _PATH_RE.match(section[0]):
            file_sections += 1
        replacement, path = _summarise_section(section)
        kept.extend(replacement)
        if path is not None:
            omitted.append(path)

    # Withholding bodies is justified by "leave room for the code" -- and
    # that warrant does not exist when there is no code. A PR that only
    # regenerates a lockfile would arrive as a single placeholder, and a
    # reviewer with nothing to read returns PASS: a dependency substitution
    # waved through precisely because the one file that mattered is the one
    # we hid. Nothing was competing for the budget either, so the saving
    # would have been zero. Hand back the diff and let the truncation path
    # describe its size honestly.
    if omitted and len(omitted) == file_sections:
        return diff, []

    return "\n".join(kept), omitted


def summarise_noisy_files(diff: str) -> str:
    return summarise_with_report(diff)[0]
