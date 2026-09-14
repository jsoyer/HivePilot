"""HP-117 local package-tree taxonomy — logical, tenant-scoped, no cloud.

OpenSpace package-tree / ``local_category_path`` pattern, rewritten in
Python. This module does **not** vendor OpenSpace, talk to a remote
skill host, persist pickle embeddings, write ``.skill_id`` sidecars, or
relocate skill files. The OpenSpace disk-layout helper is intentionally
absent: there is no function that materializes a category tree on disk.

Contracts:

- Placement is a tenant-scoped mapping ``logical_id → category_path``.
  Reclassify updates that mapping only. HP-98 directory skills stay
  where they were scanned.
- One clear classifier path assigns. Ambiguous output (multiple paths,
  low confidence, empty/invalid, or an explicit flag) is
  ``needs_review`` — never a silent assign. Review opens a HP-97 PASS
  card (``kind=skill_evolution``, ``action=taxonomy_review``).
- No cloud browse / auth / upload / import surface.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hivepilot.pass_store import (
    PENDING,
    SKILL_EVOLUTION_KIND,
    PassProposal,
    create_pending,
    inbox,
)
from hivepilot.services import db, events, state_service
from hivepilot.skill_catalog import SkillCatalogError, logical_skill_id

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
PLACEMENT_STATUSES: tuple[str, ...] = ("unplaced", "assigned", "needs_review")
UNPLACED = "unplaced"
ASSIGNED = "assigned"
NEEDS_REVIEW = "needs_review"

TAXONOMY_REVIEW_ACTION = "taxonomy_review"
SKILL_TAXONOMY_ENTITY_TYPE = "skill_taxonomy"
DEFAULT_CONFIDENCE_THRESHOLD = 0.6

_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")


class SkillTaxonomyError(ValueError):
    """Invalid category path, classifier payload, or placement identity."""


@dataclass(frozen=True)
class Classification:
    """Interpreted classifier output. ``ambiguous`` forbids silent assign."""

    paths: tuple[str, ...]
    confidence: float | None = None
    ambiguous: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SkillPlacement:
    """One tenant's logical category mapping for a skill."""

    logical_id: str
    skill_name: str
    tenant: str
    category_path: str
    status: str
    candidates: tuple[str, ...] = ()
    reason: str = ""
    known: bool = False
    proposal_id: str = ""

    @property
    def assigned(self) -> bool:
        return self.known and self.status == ASSIGNED and bool(self.category_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "skill_name": self.skill_name,
            "tenant": self.tenant,
            "category_path": self.category_path,
            "status": self.status,
            "candidates": list(self.candidates),
            "reason": self.reason,
            "known": self.known,
            "assigned": self.assigned,
            "proposal_id": self.proposal_id,
        }


@dataclass(frozen=True)
class PlacementDecision:
    """Outcome of an explicit assign or a classifier placement."""

    action: str
    placement: SkillPlacement
    classification: Classification | None = None
    proposal_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "placement": self.placement.to_dict(),
            "classification": None
            if self.classification is None
            else self.classification.to_dict(),
            "proposal_id": self.proposal_id,
        }


@dataclass(frozen=True)
class PackageNode:
    """One logical package-tree node. Built from mappings, not directories."""

    path: str
    name: str
    children: tuple["PackageNode", ...] = ()
    placements: tuple[SkillPlacement, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "children": [child.to_dict() for child in self.children],
            "placements": [row.to_dict() for row in self.placements],
        }


def confidence_threshold(override: float | None = None) -> float:
    """Minimum classifier confidence required to assign without review."""
    if override is not None:
        return min(1.0, max(0.0, float(override)))
    raw = (os.environ.get("HIVEPILOT_SKILL_TAXONOMY_CONFIDENCE") or "").strip()
    if raw:
        try:
            return min(1.0, max(0.0, float(raw)))
        except ValueError as exc:
            raise SkillTaxonomyError(
                f"HIVEPILOT_SKILL_TAXONOMY_CONFIDENCE must be a float, got {raw!r}"
            ) from exc
    return DEFAULT_CONFIDENCE_THRESHOLD


def _tenant(value: str | None) -> str:
    return (value or "default").strip() or "default"


def _identity(name_or_id: str, skill_name: str = "") -> tuple[str, str]:
    raw = (name_or_id or "").strip()
    name = (skill_name or "").strip()
    if not raw and not name:
        raise SkillTaxonomyError("logical_id or skill_name is required")
    if raw and "__" in raw and not name:
        return raw, raw.split("__", 1)[0]
    if name:
        return logical_skill_id(name), name
    try:
        return logical_skill_id(raw), raw
    except SkillCatalogError as exc:
        raise SkillTaxonomyError("logical_id or skill_name is required") from exc


def normalize_category_path(path: str) -> str:
    """Slash-separated logical path. Rejects traversal and empty segments."""
    cleaned = (path or "").strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise SkillTaxonomyError("category path is required")
    segments: list[str] = []
    for raw in cleaned.split("/"):
        segment = raw.strip().casefold()
        if not segment or segment in {".", ".."} or not _SEGMENT_RE.fullmatch(segment):
            raise SkillTaxonomyError(f"invalid category path segment {raw!r}")
        segments.append(segment)
    return "/".join(segments)


def _as_path_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value if str(item).strip()]
    raise SkillTaxonomyError("classifier paths must be a string or a sequence of strings")


def interpret_classifier(
    output: str | Mapping[str, Any] | Classification,
    *,
    threshold: float | None = None,
) -> Classification:
    """Turn classifier output into assign vs ``needs_review``.

    Ambiguous when the payload is flagged, empty, invalid, lists more
    than one path, or reports confidence below the threshold.
    """
    if isinstance(output, Classification):
        if output.ambiguous or len(output.paths) != 1:
            return Classification(
                paths=output.paths,
                confidence=output.confidence,
                ambiguous=True,
                reason=output.reason or ("multiple" if len(output.paths) > 1 else "empty"),
            )
        needed = confidence_threshold(threshold)
        if output.confidence is not None and output.confidence < needed:
            return Classification(
                paths=output.paths,
                confidence=output.confidence,
                ambiguous=True,
                reason="low_confidence",
            )
        return Classification(
            paths=output.paths,
            confidence=output.confidence,
            ambiguous=False,
            reason=output.reason or "unique",
        )

    if isinstance(output, str):
        try:
            return Classification(
                paths=(normalize_category_path(output),),
                confidence=1.0,
                ambiguous=False,
                reason="unique",
            )
        except SkillTaxonomyError:
            return Classification(paths=(), ambiguous=True, reason="invalid")

    if not isinstance(output, Mapping):
        raise SkillTaxonomyError("classifier output must be a path string or mapping")

    flagged = bool(output.get("ambiguous") or output.get("needs_review"))
    raw_paths = (
        _as_path_list(output.get("paths"))
        or _as_path_list(output.get("candidates"))
        or _as_path_list(output.get("category_path"))
        or _as_path_list(output.get("path"))
    )
    accepted: list[str] = []
    seen: set[str] = set()
    invalid = False
    for raw in raw_paths:
        try:
            path = normalize_category_path(raw)
        except SkillTaxonomyError:
            invalid = True
            continue
        if path not in seen:
            seen.add(path)
            accepted.append(path)

    confidence_raw = output.get("confidence", output.get("score"))
    confidence: float | None
    if confidence_raw is None or confidence_raw == "":
        confidence = None
    else:
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise SkillTaxonomyError(
                f"classifier confidence must be a number, got {confidence_raw!r}"
            ) from exc

    reason = str(output.get("reason") or "").strip()
    if flagged:
        return Classification(
            paths=tuple(accepted),
            confidence=confidence,
            ambiguous=True,
            reason=reason or "flagged",
        )
    if invalid and not accepted:
        return Classification(paths=(), confidence=confidence, ambiguous=True, reason="invalid")
    if not accepted:
        return Classification(paths=(), confidence=confidence, ambiguous=True, reason="empty")
    if len(accepted) > 1:
        return Classification(
            paths=tuple(accepted),
            confidence=confidence,
            ambiguous=True,
            reason=reason or "multiple",
        )
    needed = confidence_threshold(threshold)
    if confidence is not None and confidence < needed:
        return Classification(
            paths=tuple(accepted),
            confidence=confidence,
            ambiguous=True,
            reason="low_confidence",
        )
    return Classification(
        paths=tuple(accepted),
        confidence=confidence,
        ambiguous=False,
        reason=reason or "unique",
    )


def _unknown(logical_id: str, *, skill_name: str, tenant: str) -> SkillPlacement:
    return SkillPlacement(
        logical_id=logical_id,
        skill_name=skill_name,
        tenant=tenant,
        category_path="",
        status=UNPLACED,
        known=False,
    )


def get(
    name_or_id: str,
    *,
    tenant: str = "default",
    skill_name: str = "",
) -> SkillPlacement:
    """Return the stored mapping, or an unplaced view (not persisted)."""
    logical, name = _identity(name_or_id, skill_name)
    tenant_key = _tenant(tenant)
    state_service.init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM skill_taxonomy_placements WHERE tenant = ? AND logical_id = ?"),
            (tenant_key, logical),
        ).fetchone()
    if row is None:
        return _unknown(logical, skill_name=name, tenant=tenant_key)
    return _row_to_placement(row)


def list_placements(
    *,
    tenant: str = "default",
    status: str | None = None,
    category_path: str | None = None,
) -> tuple[SkillPlacement, ...]:
    """Tenant-scoped mappings, optionally filtered by status or path prefix."""
    tenant_key = _tenant(tenant)
    state_service.init_db()
    clauses = ["tenant = ?"]
    params: list[Any] = [tenant_key]
    if status is not None and status != "":
        cleaned = status.strip()
        if cleaned not in PLACEMENT_STATUSES:
            raise SkillTaxonomyError(
                f"unknown placement status {status!r}; must be one of {list(PLACEMENT_STATUSES)}"
            )
        clauses.append("status = ?")
        params.append(cleaned)
    if category_path is not None and str(category_path).strip():
        prefix = normalize_category_path(str(category_path))
        clauses.append("(category_path = ? OR category_path LIKE ?)")
        params.extend([prefix, f"{prefix}/%"])
    where = " AND ".join(clauses)
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                f"""
                SELECT * FROM skill_taxonomy_placements
                WHERE {where}
                ORDER BY category_path, skill_name, logical_id
                """
            ),
            tuple(params),
        ).fetchall()
    return tuple(_row_to_placement(row) for row in rows)


def tree(*, tenant: str = "default") -> PackageNode:
    """Logical package tree for one tenant. No filesystem walk."""
    placements = [
        row
        for row in list_placements(tenant=tenant)
        if row.category_path and row.status in {ASSIGNED, NEEDS_REVIEW}
    ]
    return _build_tree(placements)


def assign(
    name_or_id: str,
    category_path: str,
    *,
    tenant: str = "default",
    skill_name: str = "",
) -> SkillPlacement:
    """Explicit HITL/operator assign. Updates the mapping only."""
    path = normalize_category_path(category_path)
    return _write_placement(
        name_or_id,
        category_path=path,
        status=ASSIGNED,
        candidates=(),
        reason="assigned",
        tenant=tenant,
        skill_name=skill_name,
        event_kind="skill.taxonomy_assigned",
    )


def reclassify(
    name_or_id: str,
    category_path: str,
    *,
    tenant: str = "default",
    skill_name: str = "",
) -> SkillPlacement:
    """Move a skill in the logical tree. Never relocates files on disk."""
    path = normalize_category_path(category_path)
    current = get(name_or_id, tenant=tenant, skill_name=skill_name)
    event = "skill.taxonomy_reclassified" if current.known else "skill.taxonomy_assigned"
    return _write_placement(
        name_or_id,
        category_path=path,
        status=ASSIGNED,
        candidates=(),
        reason="reclassified",
        tenant=tenant,
        skill_name=skill_name,
        event_kind=event,
    )


def place(
    name_or_id: str,
    output: str | Mapping[str, Any] | Classification,
    *,
    tenant: str = "default",
    skill_name: str = "",
    threshold: float | None = None,
) -> PlacementDecision:
    """Apply classifier output. Ambiguous results stay ``needs_review``."""
    classified = interpret_classifier(output, threshold=threshold)
    current = get(name_or_id, tenant=tenant, skill_name=skill_name)
    if classified.ambiguous:
        proposal = _open_review(
            current,
            candidates=classified.paths,
            reason=classified.reason,
        )
        stored = _write_placement(
            name_or_id,
            category_path=current.category_path,
            status=NEEDS_REVIEW,
            candidates=classified.paths,
            reason=classified.reason,
            tenant=tenant,
            skill_name=skill_name or current.skill_name,
            event_kind="skill.taxonomy_review",
            extra={"proposal_id": proposal.id, "candidates": list(classified.paths)},
        )
        return PlacementDecision(
            action="review",
            placement=stored,
            classification=classified,
            proposal_id=proposal.id,
        )
    stored = assign(
        name_or_id,
        classified.paths[0],
        tenant=tenant,
        skill_name=skill_name or current.skill_name,
    )
    return PlacementDecision(
        action="assign",
        placement=stored,
        classification=classified,
    )


def _write_placement(
    name_or_id: str,
    *,
    category_path: str,
    status: str,
    candidates: Sequence[str],
    reason: str,
    tenant: str,
    skill_name: str,
    event_kind: str,
    extra: Mapping[str, Any] | None = None,
) -> SkillPlacement:
    if status not in PLACEMENT_STATUSES:
        raise SkillTaxonomyError(f"unknown placement status {status!r}")
    logical, name = _identity(name_or_id, skill_name)
    tenant_key = _tenant(tenant)
    unique_candidates = tuple(dict.fromkeys(candidates))
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_taxonomy_placements
                    (tenant, logical_id, skill_name, category_path, status,
                     candidates, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant, logical_id) DO UPDATE SET
                    skill_name = excluded.skill_name,
                    category_path = excluded.category_path,
                    status = excluded.status,
                    candidates = excluded.candidates,
                    reason = excluded.reason,
                    updated_ts = CURRENT_TIMESTAMP
                """
            ),
            (
                tenant_key,
                logical,
                name,
                category_path,
                status,
                json.dumps(list(unique_candidates), ensure_ascii=False),
                reason,
            ),
        )
    stored = get(logical, tenant=tenant_key, skill_name=name)
    payload = {
        "skill_name": stored.skill_name,
        "logical_id": stored.logical_id,
        "category_path": stored.category_path,
        "status": stored.status,
        "reason": stored.reason,
        "candidates": list(stored.candidates),
        **dict(extra or {}),
    }
    events.emit(
        event_kind,
        SKILL_TAXONOMY_ENTITY_TYPE,
        stored.logical_id,
        tenant=tenant_key,
        payload=payload,
    )
    return stored


def _open_review(
    placement: SkillPlacement,
    *,
    candidates: Sequence[str],
    reason: str,
) -> PassProposal:
    """Idempotent PENDING card. Does not call ``submit`` (no auto-decide)."""
    body = {
        "logical_id": placement.logical_id,
        "skill_name": placement.skill_name,
        "category_path": placement.category_path,
        "candidates": list(candidates),
        "reason": reason or "ambiguous",
    }
    for proposal in inbox(kind=SKILL_EVOLUTION_KIND, status=PENDING, tenant=placement.tenant):
        if (
            proposal.action == TAXONOMY_REVIEW_ACTION
            and str(proposal.payload.get("logical_id") or "") == placement.logical_id
        ):
            return proposal
    stored = create_pending(
        kind=SKILL_EVOLUTION_KIND,
        action=TAXONOMY_REVIEW_ACTION,
        project=placement.skill_name,
        task=placement.logical_id,
        payload=body,
        tenant=placement.tenant,
    )
    return stored


def _row_to_placement(row: Mapping[str, Any]) -> SkillPlacement:
    data = dict(row)
    raw_candidates = data.get("candidates") or "[]"
    if isinstance(raw_candidates, str):
        parsed = json.loads(raw_candidates)
    else:
        parsed = list(raw_candidates)
    candidates = tuple(str(item) for item in parsed)
    return SkillPlacement(
        logical_id=str(data["logical_id"]),
        skill_name=str(data.get("skill_name") or ""),
        tenant=str(data["tenant"]),
        category_path=str(data.get("category_path") or ""),
        status=str(data.get("status") or UNPLACED),
        candidates=candidates,
        reason=str(data.get("reason") or ""),
        known=True,
    )


def _build_tree(placements: Sequence[SkillPlacement]) -> PackageNode:
    buckets: dict[str, list[SkillPlacement]] = {}
    child_names: dict[str, set[str]] = {"": set()}
    for row in placements:
        segments = row.category_path.split("/")
        prefix = ""
        for segment in segments:
            parent = prefix
            prefix = segment if not prefix else f"{prefix}/{segment}"
            child_names.setdefault(parent, set()).add(segment)
            child_names.setdefault(prefix, set())
        buckets.setdefault(row.category_path, []).append(row)

    def build(path: str, name: str) -> PackageNode:
        children = tuple(
            build(f"{path}/{child}" if path else child, child)
            for child in sorted(child_names.get(path, ()))
        )
        return PackageNode(
            path=path,
            name=name,
            children=children,
            placements=tuple(buckets.get(path, ())),
        )

    roots = tuple(build(name, name) for name in sorted(child_names.get("", ())))
    return PackageNode(path="", name="", children=roots)
