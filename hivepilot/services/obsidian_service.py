"""
Obsidian vault service — safe, dry-run-first I/O wrapper.

Safety invariants:
- write_note() targets ONLY the ``12 - HivePilot/`` subtree.
- write_adr() targets ONLY the ``03 - Decisions/`` folder.
- Audit is always read-only regardless of dry_run.
- dry_run=True (default) returns planned path + content WITHOUT writing.
- Never renames or deletes folders.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

# Custom YAML Dumper that emits date-like strings (YYYY-MM-DD) without quotes.
# PyYAML SafeDumper wraps strings matching the ISO-8601 timestamp pattern in
# single quotes, which would break the ``created: 2026-06-18`` frontmatter
# convention.  The fix is to strip the ``tag:yaml.org,2002:timestamp`` resolver
# from the implicit resolver table so those strings are treated as plain str.


class _FrontmatterDumper(yaml.SafeDumper):
    """SafeDumper variant that never quotes date-like strings."""


# Build the resolver table without the timestamp tag so YYYY-MM-DD strings
# are emitted as plain scalars instead of being auto-quoted.
_FrontmatterDumper.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeDumper.yaml_implicit_resolvers.items()
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HIVEPILOT_SUBTREE = "12 - HivePilot"
ADR_TARGET_FOLDER = "03 - Decisions"

SUBTREE_FOLDERS: list[str] = ["Agents", "Tasks", "Reports", "Runs", "Interactions"]

FROZEN_FOLDERS: list[str] = [
    "08 - Security",
    "03 - Decisions",
    "02 - Architecture",
    "01 - Journal",
]

EXPECTED_TOP_LEVEL_FOLDERS: list[str] = [
    "00 - Inbox",
    "01 - Journal",
    "01 - Knowledge",
    "02 - Architecture",
    "02 - Design",
    "03 - Decisions",
    "03 - Research",
    "04 - Engineering",
    "04 - Integrations",
    "04 - PRDs",
    "04 - Roadmap",
    "05 - Competitive Intel",
    "05 - GTM",
    "06 - GTM",
    "07 - Infrastructure",
    "08 - Security",
    "09 - People",
    "10 - Legal & Compliance",
    "10 - Templates",
    "11 - Projects",
    "12 - HivePilot",
    "99 - Archive",
]

REQUIRED_FRONTMATTER_FIELDS: list[str] = [
    "title",
    "type",
    "status",
    "created",
    "agent",
    "language",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ObsidianWriteError(ValueError):
    """Raised when a write operation is rejected by the safety guard."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ObsidianService:
    """Safe I/O wrapper around an Obsidian vault directory.

    Parameters
    ----------
    vault_path:
        Absolute path to the vault root (e.g. ``/path/to/Acme``).
    dry_run:
        When ``True`` (default), no files are written.  All mutating methods
        return a dict describing the planned operation instead.
    """

    def __init__(self, vault_path: Path | str, dry_run: bool = True) -> None:
        self._vault = Path(vault_path).expanduser().resolve()
        self._dry_run = dry_run

    # ------------------------------------------------------------------
    # Public read-only
    # ------------------------------------------------------------------

    def audit(self) -> dict[str, Any]:
        """Scan the vault and return a structured report.

        Always read-only — ignores ``dry_run``.

        Returns
        -------
        dict with keys:
            ``present``  — top-level folders that exist.
            ``missing``  — expected top-level folders that are absent.
            ``frozen``   — folders that must never be renamed/deleted (full list).
            ``hivepilot_subtree`` — dict with key ``exists`` (bool) and one
                bool per expected subtree folder (Agents, Tasks, …).
        """
        present: list[str] = []
        missing: list[str] = []

        for folder in EXPECTED_TOP_LEVEL_FOLDERS:
            if (self._vault / folder).is_dir():
                present.append(folder)
            else:
                missing.append(folder)

        # Frozen folders are always flagged (by policy, regardless of whether present)
        frozen_full = list(FROZEN_FOLDERS)

        hivepilot_dir = self._vault / HIVEPILOT_SUBTREE
        subtree: dict[str, Any] = {"exists": hivepilot_dir.is_dir()}
        for sub in SUBTREE_FOLDERS:
            subtree[sub] = (hivepilot_dir / sub).is_dir()

        return {
            "present": present,
            "missing": missing,
            "frozen": frozen_full,
            "hivepilot_subtree": subtree,
        }

    # ------------------------------------------------------------------
    # Frontmatter helpers
    # ------------------------------------------------------------------

    def render_frontmatter(self, fields: dict[str, Any]) -> str:
        """Render a YAML frontmatter block.

        Enforces ``language: en`` regardless of what the caller passes.
        Field order: required fields first (in spec order), then optional extras.

        Parameters
        ----------
        fields:
            Dict of frontmatter key/value pairs.  ``language`` is always
            overridden to ``"en"``.

        Returns
        -------
        str
            A string starting with ``---\\n`` and ending with ``---``.
        """
        merged: dict[str, Any] = {**fields, "language": "en"}

        # Build ordered output: required fields first, then extras
        ordered: dict[str, Any] = {}
        for key in REQUIRED_FRONTMATTER_FIELDS:
            if key in merged:
                ordered[key] = merged[key]
        for key, value in merged.items():
            if key not in ordered:
                ordered[key] = value

        yaml_body = yaml.dump(
            ordered,
            default_flow_style=False,
            allow_unicode=True,
            Dumper=_FrontmatterDumper,
        ).rstrip()
        return f"---\n{yaml_body}\n---"

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def write_note(
        self,
        subpath: str,
        title: str,
        body: str,
        frontmatter_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Write a note under the ``12 - HivePilot/`` subtree.

        Parameters
        ----------
        subpath:
            Path relative to ``12 - HivePilot/``, e.g. ``Tasks/2026-06-18-my-task.md``.
        title:
            Human-readable title (injected into frontmatter).
        body:
            Markdown body content (appended after the frontmatter block).
        frontmatter_fields:
            Fields merged into the frontmatter.  ``title`` and ``language``
            are always set/overridden.

        Returns
        -------
        dict with keys ``path`` (str), ``content`` (str), ``dry_run`` (bool).

        Raises
        ------
        ObsidianWriteError
            If the resolved path escapes the ``12 - HivePilot/`` subtree.
        """
        allowed_root = (self._vault / HIVEPILOT_SUBTREE).resolve()
        target = _resolve_safe(allowed_root, subpath, context="write_note")

        merged_fields: dict[str, Any] = {**frontmatter_fields, "title": title}
        frontmatter = self.render_frontmatter(merged_fields)
        content = f"{frontmatter}\n\n{body}\n"

        return self._emit(target, content)

    def write_adr(
        self,
        title: str,
        context: str,
        options: list[str],
        decision: str,
        consequences: str,
        security_impact: str,
        review_date: str,
    ) -> dict[str, Any]:
        """Write an Architecture Decision Record under ``03 - Decisions/``.

        Parameters
        ----------
        title:
            Short ADR title (used for frontmatter and heading).
        context:
            Background and forces at play.
        options:
            List of options considered.
        decision:
            The chosen option and rationale.
        consequences:
            Positive and negative consequences.
        security_impact:
            Security implications of the decision.
        review_date:
            ISO date for the next scheduled review (``YYYY-MM-DD``).

        Returns
        -------
        dict with keys ``path`` (str), ``content`` (str), ``dry_run`` (bool).
        """
        allowed_root = (self._vault / ADR_TARGET_FOLDER).resolve()
        today = datetime.date.today().isoformat()
        safe_title = _slugify(title)
        filename = f"{today}-{safe_title}.md"
        target = _resolve_safe(allowed_root, filename, context="write_adr")

        options_md = "\n".join(f"- {opt}" for opt in options)
        body = (
            f"# {title}\n\n"
            f"## Status:\n\ndraft\n\n"
            f"## Context:\n\n{context}\n\n"
            f"## Options:\n\n{options_md}\n\n"
            f"## Decision:\n\n{decision}\n\n"
            f"## Consequences:\n\n{consequences}\n\n"
            f"## Security Impact:\n\n{security_impact}\n\n"
            f"## Review Date:\n\n{review_date}\n"
        )

        frontmatter_fields: dict[str, Any] = {
            "title": title,
            "type": "adr",
            "status": "draft",
            "created": today,
            "agent": "hivepilot",
        }
        frontmatter = self.render_frontmatter(frontmatter_fields)
        content = f"{frontmatter}\n\n{body}"

        return self._emit(target, content)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, target: Path, content: str) -> dict[str, Any]:
        """Write content to target (or skip if dry_run)."""
        if self._dry_run:
            return {"path": str(target), "content": content, "dry_run": True}

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "content": content, "dry_run": False}


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------


def _resolve_safe(allowed_root: Path, subpath: str, context: str) -> Path:
    """Resolve *subpath* relative to *allowed_root* and verify it stays within.

    Raises
    ------
    ObsidianWriteError
        If the resolved path escapes *allowed_root*.
    """
    # Reject absolute paths up front
    candidate_raw = Path(subpath)
    if candidate_raw.is_absolute():
        raise ObsidianWriteError(
            f"[{context}] Absolute subpath '{subpath}' is outside allowed subtree '{allowed_root}'"
        )

    resolved = (allowed_root / candidate_raw).resolve()

    try:
        resolved.relative_to(allowed_root)
    except ValueError:
        raise ObsidianWriteError(
            f"[{context}] Resolved path '{resolved}' is outside allowed subtree '{allowed_root}'"
        )

    return resolved


def _slugify(text: str, max_len: int = 80) -> str:
    """Convert a title to a lowercase-kebab-case filename slug.

    Capped to ``max_len`` chars so a long title (e.g. a full brief used as an ADR
    title) can't produce a path that exceeds the filesystem limit (Errno 36).
    """
    import re

    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug
