"""HP-98 skill catalog — read-only scan into an in-memory revision DAG.

OpenSpace pattern (``skill_engine/types.py``, ``store.py``, ``patch.py``),
rewritten in Python. This module does **not** vendor OpenSpace, write
``.skill_id`` sidecars, persist pickle embeddings, or talk to OpenSpace
cloud.

Scan is read-only: directory skills come from
``skill_dirs.discover_directory_skills``; plugin skills from
``register()["skills"]`` (passed in, or listed from a ``PluginManager``).
Stable logical ids are derived from the skill **name** — HivePilot's
existing identity — so a content-hash change creates a new revision under
the same logical skill. OpenSpace persists ``{name}__imp_{uuid}`` in a
sidecar; we refuse that write and use a deterministic digest instead.

HP-105 trust lives in ``hivepilot.skill_trust`` (provisional↔trusted,
``enabled`` orthogonal). HP-104 cycle events live in
``hivepilot.skill_events`` (idempotent per revision/run/step/type;
absence of measurement is not zero).
HP-99 evidence lives in ``hivepilot.evidence`` and may mark a
``skill_evolution`` proposal non-admissible when refs are missing.
Workshop states (proposed|accepted|rejected) stay in
``skill_workshop_service``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from hivepilot.skill_dirs import discover_directory_skills

SKILL_ID_SIDECAR = ".skill_id"

# Closed vocabulary. Adding a token is additive; renaming is a breaking change.
ORIGIN_VALUES: tuple[str, ...] = ("imported", "fixed", "derived", "captured")


class SkillOrigin(str, Enum):
    """How a revision entered the catalog.

    Version DAG — every content change is a new node, not a new logical skill::

        IMPORTED / CAPTURED → root (no parents)
        FIXED               → exactly one parent (previous revision of same skill)
        DERIVED             → one or more parents (new logical skill / name)
    """

    IMPORTED = "imported"
    FIXED = "fixed"
    DERIVED = "derived"
    CAPTURED = "captured"


class SkillCatalogError(ValueError):
    """Invalid origin, parent set, or broken active-revision invariant."""


class SkillListing(Protocol):
    """Minimal plugin surface so tests can inject a stub."""

    def list_skills(self) -> Sequence[Mapping[str, Any]]: ...


def logical_skill_id(name: str) -> str:
    """Stable logical id from the skill name. Never written to disk."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise SkillCatalogError("skill name is required")
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}__{digest}"


def snapshot_hash(files: Mapping[str, str]) -> str:
    """Canonical SHA-256 of a skill file map (sorted keys)."""
    payload = json.dumps(dict(files), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revision_id(logical_id: str, content_hash: str) -> str:
    """Content-addressed revision id for ``(logical_id, content_hash)``."""
    raw = f"{logical_id}\0{content_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _copy_files(files: Mapping[str, str] | None) -> dict[str, str]:
    if files is None:
        return {}
    copied: dict[str, str] = {}
    for rel, content in files.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise SkillCatalogError("skill files must be str → str")
        copied[rel] = content
    return copied


def _origin(value: SkillOrigin | str) -> SkillOrigin:
    if isinstance(value, SkillOrigin):
        return value
    try:
        return SkillOrigin(value)
    except ValueError as exc:
        raise SkillCatalogError(
            f"unknown origin {value!r}; must be one of {list(ORIGIN_VALUES)}"
        ) from exc


@dataclass(frozen=True)
class SkillRevision:
    """One DAG node. ``is_active`` is true for at most one node per logical skill."""

    revision_id: str
    logical_id: str
    name: str
    origin: SkillOrigin
    content_hash: str
    parent_revision_ids: tuple[str, ...]
    generation: int
    files: Mapping[str, str]
    provider: str = ""
    description: str = ""
    is_active: bool = True

    def deactivate(self) -> SkillRevision:
        return replace(self, is_active=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "logical_id": self.logical_id,
            "name": self.name,
            "origin": self.origin.value,
            "content_hash": self.content_hash,
            "parent_revision_ids": list(self.parent_revision_ids),
            "generation": self.generation,
            "files": dict(self.files),
            "provider": self.provider,
            "description": self.description,
            "is_active": self.is_active,
        }


@dataclass
class LogicalSkill:
    """One logical skill and the revisions that share its id."""

    logical_id: str
    name: str
    revisions: list[SkillRevision] = field(default_factory=list)

    @property
    def active_revision(self) -> SkillRevision:
        active = [rev for rev in self.revisions if rev.is_active]
        if len(active) != 1:
            raise SkillCatalogError(
                f"{self.logical_id} must have exactly one active revision, got {len(active)}"
            )
        return active[0]

    @property
    def active_revision_id(self) -> str:
        return self.active_revision.revision_id


class SkillCatalog:
    """In-memory catalog. ``scan`` never writes ``.skill_id`` or any other file."""

    def __init__(self) -> None:
        self._skills: dict[str, LogicalSkill] = {}

    def __len__(self) -> int:
        return len(self._skills)

    def logical_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))

    def get(self, logical_id: str) -> LogicalSkill | None:
        return self._skills.get(logical_id)

    def get_by_name(self, name: str) -> LogicalSkill | None:
        return self._skills.get(logical_skill_id(name))

    def active_revision(self, name_or_id: str) -> SkillRevision | None:
        skill = self._skills.get(name_or_id) or self.get_by_name(name_or_id)
        if skill is None:
            return None
        return skill.active_revision

    def list_active(self) -> tuple[SkillRevision, ...]:
        return tuple(self._skills[key].active_revision for key in sorted(self._skills))

    def revisions(self, name_or_id: str) -> tuple[SkillRevision, ...]:
        skill = self._skills.get(name_or_id) or self.get_by_name(name_or_id)
        if skill is None:
            return ()
        return tuple(skill.revisions)

    def scan(
        self,
        *,
        base_dir: Path | None = None,
        plugin_manager: SkillListing | None = None,
        plugin_skills: Iterable[Mapping[str, Any]] | None = None,
    ) -> SkillCatalog:
        """Read existing skill sources into this catalog. No disk writes.

        When ``plugin_manager`` is given, its ``list_skills()`` is the merged
        view (plugins already win over directories). Otherwise directory
        skills are discovered via ``discover_directory_skills`` and optional
        ``plugin_skills`` win on a name clash — same rule as PluginManager.
        """
        if plugin_manager is not None:
            for spec in plugin_manager.list_skills():
                self.ingest(spec, origin=SkillOrigin.IMPORTED)
            return self

        plugins = list(plugin_skills or ())
        plugin_names = {str(spec.get("name") or "") for spec in plugins}
        for spec in plugins:
            self.ingest(spec, origin=SkillOrigin.IMPORTED)
        for spec in discover_directory_skills(base_dir=base_dir):
            if spec["name"] in plugin_names:
                continue
            self.ingest(spec, origin=SkillOrigin.IMPORTED)
        return self

    def ingest(
        self,
        spec: Mapping[str, Any],
        *,
        origin: SkillOrigin | str = SkillOrigin.IMPORTED,
        parent_logical_ids: Sequence[str] = (),
    ) -> SkillRevision:
        """Add or refresh one SkillSpec-like mapping.

        First sight of a name is a root (IMPORTED / CAPTURED / DERIVED).
        A later content-hash change on the same name is FIXED — same
        logical id, new revision, previous revision deactivated.
        """
        name = str(spec.get("name") or "").strip()
        if not name:
            raise SkillCatalogError("skill spec is missing a name")
        files = _copy_files(spec.get("files") or {})
        description = str(spec.get("description") or "")
        provider = str(spec.get("provider") or "")
        return self.record(
            name=name,
            files=files,
            origin=origin,
            description=description,
            provider=provider,
            parent_logical_ids=parent_logical_ids,
        )

    def record(
        self,
        *,
        name: str,
        files: Mapping[str, str],
        origin: SkillOrigin | str = SkillOrigin.IMPORTED,
        description: str = "",
        provider: str = "",
        parent_logical_ids: Sequence[str] = (),
    ) -> SkillRevision:
        """Record a snapshot. Content change ⇒ new revision, same logical id."""
        resolved = _origin(origin)
        logical_id = logical_skill_id(name)
        files_copy = _copy_files(files)
        digest = snapshot_hash(files_copy)
        existing = self._skills.get(logical_id)

        if existing is not None:
            active = existing.active_revision
            if active.content_hash == digest:
                return active
            # Same logical skill, new bytes: FIXED, even if the caller
            # passed IMPORTED (a rescan after an on-disk edit).
            return self._append_fixed(
                existing,
                files=files_copy,
                content_hash=digest,
                description=description or active.description,
                provider=provider or active.provider,
            )

        if resolved is SkillOrigin.FIXED:
            raise SkillCatalogError(f"FIXED requires an existing logical skill {logical_id!r}")
        if resolved is SkillOrigin.DERIVED and not parent_logical_ids:
            raise SkillCatalogError("DERIVED requires one or more parent logical skills")
        if resolved in (SkillOrigin.IMPORTED, SkillOrigin.CAPTURED) and parent_logical_ids:
            raise SkillCatalogError(f"{resolved.value} is a root origin (no parents)")

        parent_revs, generation = self._resolve_parents(resolved, parent_logical_ids)
        rev = SkillRevision(
            revision_id=revision_id(logical_id, digest),
            logical_id=logical_id,
            name=name,
            origin=resolved,
            content_hash=digest,
            parent_revision_ids=parent_revs,
            generation=generation,
            files=files_copy,
            provider=provider,
            description=description,
            is_active=True,
        )
        self._skills[logical_id] = LogicalSkill(logical_id=logical_id, name=name, revisions=[rev])
        return rev

    def _append_fixed(
        self,
        skill: LogicalSkill,
        *,
        files: Mapping[str, str],
        content_hash: str,
        description: str,
        provider: str,
    ) -> SkillRevision:
        active = skill.active_revision
        new = SkillRevision(
            revision_id=revision_id(skill.logical_id, content_hash),
            logical_id=skill.logical_id,
            name=skill.name,
            origin=SkillOrigin.FIXED,
            content_hash=content_hash,
            parent_revision_ids=(active.revision_id,),
            generation=active.generation + 1,
            files=files,
            provider=provider,
            description=description,
            is_active=True,
        )
        skill.revisions = [rev.deactivate() if rev.is_active else rev for rev in skill.revisions]
        skill.revisions.append(new)
        # Invariant: exactly one active after the swap.
        _ = skill.active_revision
        return new

    def _resolve_parents(
        self,
        origin: SkillOrigin,
        parent_logical_ids: Sequence[str],
    ) -> tuple[tuple[str, ...], int]:
        if origin in (SkillOrigin.IMPORTED, SkillOrigin.CAPTURED):
            return (), 0
        parent_revs: list[str] = []
        generations: list[int] = []
        for raw in parent_logical_ids:
            parent = self._skills.get(raw) or self.get_by_name(raw)
            if parent is None:
                raise SkillCatalogError(f"unknown parent logical skill {raw!r}")
            active = parent.active_revision
            parent_revs.append(active.revision_id)
            generations.append(active.generation)
        if origin is SkillOrigin.FIXED and len(parent_revs) != 1:
            raise SkillCatalogError("FIXED requires exactly one parent")
        generation = (max(generations) + 1) if generations else 0
        return tuple(parent_revs), generation


def scan_catalog(
    *,
    base_dir: Path | None = None,
    plugin_manager: SkillListing | None = None,
    plugin_skills: Iterable[Mapping[str, Any]] | None = None,
    catalog: SkillCatalog | None = None,
) -> SkillCatalog:
    """Scan skill sources into a catalog (new, or appended to *catalog*)."""
    target = catalog if catalog is not None else SkillCatalog()
    return target.scan(
        base_dir=base_dir,
        plugin_manager=plugin_manager,
        plugin_skills=plugin_skills,
    )
