"""
Obsidian vault service — safe, dry-run-first I/O wrapper.

Safety invariants:
- write_note() targets ONLY the ``hivepilot`` folder slot's subtree.
- write_adr() targets ONLY the ``decisions`` folder slot.
- write_artifact() targets ONLY the ``artifacts`` folder slot.
- Audit is always read-only regardless of dry_run.
- dry_run=True (default) returns planned path + content WITHOUT writing.
- Never renames or deletes folders.

These three folders are the ONLY places HivePilot writes in a vault. Both the
vault root AND the folder names are configurable, and neither is hardcoded here:

- the root, globally via ``HIVEPILOT_OBSIDIAN_VAULT`` or per project via
  ``obsidian_vault:`` in projects.yaml (see
  ``hivepilot.services.obsidian_vault_resolver``);
- the folder NAMES via the ``folders:`` key of ``vault.yaml`` (see
  ``hivepilot.services.vault_layout``), because a vault's filing convention
  belongs to the organisation that owns the vault, not to the engine.

This class stays vault-agnostic: callers pass the already-resolved root as
``vault_path``, and may pass an explicit ``layout`` instead of the
deployment-resolved one. An unconfigured write slot REFUSES the write
(``ObsidianWriteError``) — it never falls back to the vault root and never
creates a folder name the engine guessed. The full layout — which writer targets
which slot, and how the audit's expected-layout list stays independent of the
write targets — is documented in ``docs/INTEGRATIONS.md`` under "Obsidian →
Vault layout".
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import yaml

from hivepilot.services import vault_layout
from hivepilot.services.vault_layout import (
    SLOT_ARTIFACTS,
    SLOT_DECISIONS,
    SLOT_HIVEPILOT,
    VAULT_FOLDER_SLOTS,
    VaultLayout,
    VaultLayoutError,
)

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

# Subfolders INSIDE the engine's own subtree. Engine-owned, not part of any
# organisation's taxonomy: HivePilot creates and names these itself, and
# `pipelines.write_stage_artifact` writes into "Runs". Deliberately NOT config-
# owned — nothing here is a customer's filing convention.
SUBTREE_FOLDERS: list[str] = ["Agents", "Tasks", "Reports", "Runs", "Interactions"]

# ---------------------------------------------------------------------------
# Two lists, two questions, deliberately NOT derived from one another
# ---------------------------------------------------------------------------
# `layout.expected_folders` is the EXPECTED LAYOUT of the vault — the operator's
# declaration of what their vault should contain. Most entries have no writer
# anywhere in the engine. `audit()` reports it present/missing.
#
# The folders HivePilot actually writes are only the `hivepilot`, `decisions` and
# `artifacts` SLOTS. Don't infer write behaviour from membership in the expected
# list, and don't compute either list from the other.
#
# That warning exists because of a real bug: the artifacts folder — the one
# folder the engine writes deliverables into — was absent from the expected list
# for several releases, so `obsidian audit` reported a complete-looking vault
# while the engine wrote somewhere the operator was never told about. The fix is
# NOT to union the two lists (that would answer "what does the operator expect?"
# with "where does the engine write?"). `audit()` instead reports the engine's
# own folders in a SEPARATE `engine_folders` section, derived from the slot
# vocabulary itself, so it is blind to none of them by construction — whatever
# the operator declared. See `audit`.
# ---------------------------------------------------------------------------

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
    layout:
        The vault folder taxonomy. ``None`` (the default) uses the
        deployment-resolved one from ``vault.yaml``; an explicit
        ``VaultLayout`` lets a caller — or a test — pin a taxonomy without
        touching global state, keeping this class as vault-agnostic as its
        ``vault_path`` parameter already makes it.
    """

    def __init__(
        self,
        vault_path: Path | str,
        dry_run: bool = True,
        layout: VaultLayout | None = None,
    ) -> None:
        self._vault = Path(vault_path).expanduser().resolve()
        self._dry_run = dry_run
        self._layout = layout if layout is not None else vault_layout.current_layout()

    # ------------------------------------------------------------------
    # Folder slot resolution
    # ------------------------------------------------------------------

    def _write_root(self, slot: str, context: str) -> Path:
        """Resolve the allowed write root for *slot*, or refuse loudly.

        An unconfigured slot raises rather than degrading to the vault root.
        ``VaultLayoutError`` is translated to ``ObsidianWriteError`` so every
        caller keeps the single "this write was refused" exception type this
        module has always raised (both subclass ``ValueError``).
        """
        try:
            folder = self._layout.require_folder(slot)
        except VaultLayoutError as exc:
            raise ObsidianWriteError(f"[{context}] {exc}") from exc
        return (self._vault / folder).resolve()

    # ------------------------------------------------------------------
    # Public read-only
    # ------------------------------------------------------------------

    def audit(self) -> dict[str, Any]:
        """Scan the vault and return a structured report.

        Always read-only — ignores ``dry_run``.

        TWO INDEPENDENT REPORTS, because there are two different questions:

        * ``present``/``missing``/``expected_examined`` answer "does this vault
          match the layout the OPERATOR declared?" — driven entirely by
          ``expected_folders:`` in ``vault.yaml``. The engine has no opinion
          here, so an operator who declared nothing gets
          ``expected_examined == 0``, which callers MUST surface as "nothing was
          checked" rather than as a clean bill of health (``hivepilot obsidian
          audit`` prints it and ``--strict`` exits non-zero).
        * ``engine_folders`` answers "where does HivePilot itself read and
          write, and are those folders configured and present?" — derived from
          the slot vocabulary in ``vault_layout``, NOT from the expected list.
          This is what makes the audit structurally incapable of omitting a
          write target, which is the bug that hid the artifacts folder for
          several releases. Neither report is computed from the other.

        Always read-only — ignores ``dry_run``.

        Returns
        -------
        dict with keys:
            ``present``  — declared expected folders that exist.
            ``missing``  — declared expected folders that are absent.
            ``expected_examined`` — how many folders the expected-layout check
                actually looked at. ``0`` means the check established NOTHING.
            ``frozen``   — folders the operator declared must never be
                renamed/deleted (full list, regardless of presence).
            ``engine_folders`` — slot → ``{folder, access, configured, exists}``
                for every folder the engine itself touches.
            ``hivepilot_subtree`` — dict with keys ``configured`` and ``exists``
                (bool) and one bool per expected subtree folder (Agents, Tasks,
                …).
        """
        expected = list(self._layout.expected_folders)
        present: list[str] = []
        missing: list[str] = []

        for folder in expected:
            if (self._vault / folder).is_dir():
                present.append(folder)
            else:
                missing.append(folder)

        # Frozen folders are always flagged (by policy, regardless of presence).
        frozen_full = list(self._layout.frozen_folders)

        # Derived from the closed slot vocabulary, so a newly added slot appears
        # here automatically instead of waiting to be remembered.
        engine_folders: dict[str, Any] = {}
        for slot in VAULT_FOLDER_SLOTS:
            folder = self._layout.folder(slot)
            engine_folders[slot] = {
                "folder": folder,
                "access": vault_layout.SLOT_ACCESS[slot],
                "configured": bool(folder),
                # An unconfigured slot is NOT "missing" — there is no path to
                # test. Reporting False for both keeps `exists` from ever being
                # read as "the folder is there".
                "exists": bool(folder) and (self._vault / folder).is_dir(),
            }

        subtree_folder = self._layout.folder(SLOT_HIVEPILOT)
        subtree: dict[str, Any] = {
            "configured": bool(subtree_folder),
            "exists": bool(subtree_folder) and (self._vault / subtree_folder).is_dir(),
        }
        for sub in SUBTREE_FOLDERS:
            subtree[sub] = bool(subtree_folder) and (self._vault / subtree_folder / sub).is_dir()

        return {
            "present": present,
            "missing": missing,
            "expected_examined": len(expected),
            "frozen": frozen_full,
            "engine_folders": engine_folders,
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
        """Write a note under the engine's own subtree (``hivepilot`` slot).

        Parameters
        ----------
        subpath:
            Path relative to the subtree, e.g. ``Tasks/2026-06-18-my-task.md``.
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
            If the ``hivepilot`` folder slot is not configured, or the resolved
            path escapes that subtree.
        """
        allowed_root = self._write_root(SLOT_HIVEPILOT, "write_note")
        target = _resolve_safe(allowed_root, subpath, context="write_note")

        merged_fields: dict[str, Any] = {**frontmatter_fields, "title": title}
        frontmatter = self.render_frontmatter(merged_fields)
        content = f"{frontmatter}\n\n{body}\n"

        return self._emit(target, content)

    def append_daily(self, entry: str, subfolder: str = "Runs") -> dict[str, Any]:
        """Append an already-rendered markdown entry to today's daily journal note.

        Targets ``<hivepilot slot folder>/<subfolder>/<YYYY-MM-DD>.md``. Creates
        the file (with frontmatter) on the first append of the day; subsequent
        calls append to the existing body without disturbing the original
        frontmatter block.

        Parameters
        ----------
        entry:
            Already-rendered markdown text for this entry (the caller is
            responsible for timestamping the entry itself).
        subfolder:
            Subfolder relative to the ``hivepilot`` slot folder, defaults to
            ``"Runs"``.

        Returns
        -------
        dict with keys ``path`` (str), ``content`` (str, full file content
        after the append), ``dry_run`` (bool), ``created`` (bool — ``True``
        when this call creates a brand-new daily file).

        Raises
        ------
        ObsidianWriteError
            If the ``hivepilot`` folder slot is not configured, or the resolved
            path escapes that subtree.
        """
        allowed_root = self._write_root(SLOT_HIVEPILOT, "append_daily")
        today = datetime.date.today().isoformat()
        subpath = f"{subfolder}/{today}.md"
        target = _resolve_safe(allowed_root, subpath, context="append_daily")

        entry_block = entry if entry.endswith("\n") else f"{entry}\n"

        existing_content: str | None = (
            target.read_text(encoding="utf-8") if target.exists() else None
        )

        if existing_content is None:
            frontmatter_fields: dict[str, Any] = {
                "title": today,
                "type": "run-log",
                "status": "active",
                "created": today,
                "agent": "hivepilot",
            }
            frontmatter = self.render_frontmatter(frontmatter_fields)
            content = f"{frontmatter}\n\n{entry_block}"
            created = True
        else:
            content = f"{existing_content.rstrip()}\n\n{entry_block}"
            created = False

        result = self._emit(target, content)
        result["created"] = created
        return result

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
        """Write an Architecture Decision Record under the ``decisions`` folder.

        The folder name comes from the ``decisions`` slot of ``vault.yaml``.
        Unconfigured raises ``ObsidianWriteError`` — the engine will not guess a
        folder name and create it in the operator's vault.

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
        allowed_root = self._write_root(SLOT_DECISIONS, "write_adr")
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

    def write_artifact(
        self,
        role: str,
        slug: str,
        title: str,
        body: str,
        frontmatter_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Write a canonical stage-deliverable artifact under ``<artifacts>/<role>/``.

        Unlike `write_note` (scoped to the engine's own subtree), this targets
        the top-level ``artifacts`` slot folder directly — mirroring
        `write_adr`'s ``decisions`` target — so a planning agent's deliverable
        (CEO/PM/CTO/designer stage output) lands where a human browsing the
        vault expects a canonical artifact, not buried in the internal run-log
        copy under the engine's own subtree.

        The folder name comes from the ``artifacts`` slot of ``vault.yaml``.
        Unconfigured raises ``ObsidianWriteError``; `pipelines.write_stage_artifact`
        treats that as a best-effort miss and logs it, so a run is never failed
        by an undeclared taxonomy — but nothing is written to a guessed folder.

        Parameters
        ----------
        role:
            The stage's producing role (e.g. ``"cto"``) — becomes a subfolder
            under the artifacts folder. Slugified defensively so an unexpected
            role string can't escape the target folder or introduce path
            separators.
        slug:
            Deterministic filename slug (without the date prefix or ``.md``
            suffix), e.g. ``"run43-cto-technical-spec"``. Also slugified
            defensively.
        title:
            Human-readable title (injected into frontmatter).
        body:
            Markdown body content (the stage's deliverable).
        frontmatter_fields:
            Fields merged into the frontmatter. ``title`` and ``language``
            are always set/overridden.

        Returns
        -------
        dict with keys ``path`` (str), ``content`` (str), ``dry_run`` (bool).

        Raises
        ------
        ObsidianWriteError
            If the ``artifacts`` folder slot is not configured, or the resolved
            path escapes that folder.
        """
        allowed_root = self._write_root(SLOT_ARTIFACTS, "write_artifact")
        safe_role = _slugify(role) or "unknown"
        today = datetime.date.today().isoformat()
        safe_slug = _slugify(slug) or "artifact"
        subpath = f"{safe_role}/{today}-{safe_slug}.md"
        target = _resolve_safe(allowed_root, subpath, context="write_artifact")

        merged_fields: dict[str, Any] = {**frontmatter_fields, "title": title}
        frontmatter = self.render_frontmatter(merged_fields)
        content = f"{frontmatter}\n\n{body}\n"

        return self._emit(target, content)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, target: Path, content: str) -> dict[str, Any]:
        """Write content to target (or skip if dry_run).

        Choke point: `content` is the fully-rendered note (frontmatter + body)
        for EVERY vault write — write_note, append_daily, write_adr, and any
        direct caller. Bodies frequently carry agent stage output, which can
        echo a resolved ${secret:NAME} value, so redact here once rather than
        at each individual call site (idempotent — a no-op if already
        redacted upstream, e.g. by pipelines.write_stage_artifact).
        """
        from hivepilot import outward
        from hivepilot.services.config_provenance import redact_text

        content = redact_text(content)
        if self._dry_run:
            return {"path": str(target), "content": content, "dry_run": True}

        # Outward consent (`vault_write`): a partition dispatched WITHOUT
        # outward consent must not write into the operator's vault -- a
        # vault is typically a synced git repo, i.e. visible outside this
        # machine. Gated at the ONE choke point every vault write already
        # funnels through (write_note / append_daily / write_adr /
        # write_artifact and any direct caller), for the same reason the
        # redaction above lives here.
        #
        # The suppressed write returns the SAME shape a `dry_run` write
        # returns, plus an explicit `suppressed` marker, so a caller that
        # reads `["path"]` keeps working and a caller that wants to know can
        # ask. `permits` logs it; nothing is dropped silently.
        if not outward.permits(
            "vault_write",
            surface="obsidian_service.ObsidianService._emit",
            detail=str(target),
        ):
            return {
                "path": str(target),
                "content": content,
                "dry_run": True,
                "suppressed": "outward_consent",
            }

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
