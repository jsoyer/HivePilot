"""HP-99 tenant-scoped evidence refs, bounded packets, and ingest redaction.

OpenSpace ``skill_engine/evidence/*`` pattern, rewritten in Python. This
module does **not** vendor OpenSpace, talk to OpenSpace cloud, persist
pickle embeddings, or implement HP-109 FIX/DERIVED/CAPTURED apply paths.

Contracts:

- Refs are keyed by ``(tenant, ref_id)``. A foreign-tenant id is missing.
- Ingest redacts registered secret values and secret-looking metadata keys
  before anything is stored.
- Each ingest emits an HP-40 ``change_log`` row; the ref watermark **is**
  that ``change_log.id``.
- An evolution claim is admissible only when every cited ref exists in the
  same tenant. Missing / empty refs ⇒ not admissible.

``pass_store.submit(kind=skill_evolution)`` uses ``assess_evolution_claim``
to reject non-admissible proposals. ``create_pending`` stays persist-first
(HP-97). Applying an approved evolution is HP-109.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hivepilot.services import db, events, state_service
from hivepilot.services.config_provenance import REDACTED, redact_text, redact_value

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
REF_TYPES: tuple[str, ...] = (
    "runtime_snapshot",
    "skill_record",
    "skill_file",
    "tool_event",
    "transcript_message",
    "memory_ref",
    "manual_request",
)

EVIDENCE_EVENT_KIND = "evidence.ingested"
EVIDENCE_ENTITY_TYPE = "evidence_ref"

DEFAULT_MAX_PACKET_CHARS = 8000
DEFAULT_MAX_PACKET_REFS = 16
DEFAULT_SNIPPET_CHARS = 400

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|authorization|cookie|secret|password|credential)",
    re.IGNORECASE,
)
_CLAIM_REF_KEYS: tuple[str, ...] = ("evidence_refs", "refs")


class EvidenceError(ValueError):
    """Invalid evidence ref, packet, or ingest."""


@dataclass(frozen=True)
class EvidenceRef:
    """One tenant-scoped evidence pointer. ``watermark`` is ``change_log.id``."""

    ref_id: str
    tenant: str
    ref_type: str
    preview: str
    metadata: dict[str, Any]
    watermark: int
    first_seen_watermark: int
    contains_secret: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "tenant": self.tenant,
            "ref_type": self.ref_type,
            "preview": self.preview,
            "metadata": dict(self.metadata),
            "watermark": self.watermark,
            "first_seen_watermark": self.first_seen_watermark,
            "contains_secret": self.contains_secret,
        }


@dataclass(frozen=True)
class PacketBudget:
    max_chars: int
    used_chars: int
    omitted_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_chars": self.max_chars,
            "used_chars": self.used_chars,
            "omitted_refs": list(self.omitted_refs),
        }


@dataclass(frozen=True)
class EvidenceSnippet:
    ref_id: str
    text: str
    truncation: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {"ref_id": self.ref_id, "text": self.text, "truncation": self.truncation}


@dataclass(frozen=True)
class EvidencePacket:
    """Bounded, redacted assembly of already-ingested refs."""

    packet_id: str
    tenant: str
    watermark: int
    refs: tuple[EvidenceRef, ...]
    snippets: tuple[EvidenceSnippet, ...]
    budget: PacketBudget
    missing_refs: tuple[str, ...] = ()
    redaction_status: str = "redacted"
    build_status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "tenant": self.tenant,
            "watermark": self.watermark,
            "refs": [ref.to_dict() for ref in self.refs],
            "snippets": [snippet.to_dict() for snippet in self.snippets],
            "budget": self.budget.to_dict(),
            "missing_refs": list(self.missing_refs),
            "redaction_status": self.redaction_status,
            "build_status": self.build_status,
        }


@dataclass(frozen=True)
class Admissibility:
    """Evolution-claim gate. ``admissible`` is the only success flag."""

    admissible: bool
    tenant: str
    checked_refs: tuple[str, ...] = ()
    missing_refs: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "admissible": self.admissible,
            "tenant": self.tenant,
            "checked_refs": list(self.checked_refs),
            "missing_refs": list(self.missing_refs),
            "reason": self.reason,
        }


def normalize_ref_type(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned not in REF_TYPES:
        raise EvidenceError(f"ref_type must be one of {list(REF_TYPES)}; got {raw!r}")
    return cleaned


def normalize_tenant(raw: str | None) -> str:
    cleaned = (raw or "").strip()
    return cleaned or "default"


def claim_ref_ids(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Collect cited evidence ids from an evolution payload.

    Accepts ``evidence_refs`` / ``refs`` at the top level and the same keys
    on each ``claims[]`` item (OpenSpace claim shape, rewritten).
    """
    if not isinstance(payload, Mapping):
        return ()
    found: list[str] = []
    seen: set[str] = set()

    def _extend(raw: Any) -> None:
        if not isinstance(raw, (list, tuple)):
            return
        for item in raw:
            ref_id = str(item or "").strip()
            if ref_id and ref_id not in seen:
                seen.add(ref_id)
                found.append(ref_id)

    for key in _CLAIM_REF_KEYS:
        _extend(payload.get(key))
    claims = payload.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            for key in _CLAIM_REF_KEYS:
                _extend(claim.get(key))
    return tuple(found)


def redact_evidence_text(text: str) -> str:
    """Redact registered secret values from a preview string."""
    return redact_text(text or "")


def redact_evidence_metadata(value: Any) -> Any:
    """Redact registered secrets, then blank secret-looking mapping keys."""
    redacted = redact_value(value)
    return _redact_secret_keys(redacted)


def _redact_secret_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                out[key_text] = REDACTED
            else:
                out[key_text] = _redact_secret_keys(item)
        return out
    if isinstance(value, list):
        return [_redact_secret_keys(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_keys(item) for item in value]
    if isinstance(value, str):
        return redact_evidence_text(value)
    return value


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise EvidenceError("metadata must be a JSON object")
        return loaded
    raise EvidenceError("metadata must be a mapping")


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_refs (
                tenant TEXT NOT NULL DEFAULT 'default',
                ref_id TEXT NOT NULL,
                ref_type TEXT NOT NULL,
                preview TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                contains_secret INTEGER NOT NULL DEFAULT 0,
                first_seen_watermark INTEGER NOT NULL,
                last_seen_watermark INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant, ref_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_refs_tenant_watermark "
            "ON evidence_refs (tenant, last_seen_watermark)"
        )


def _row(raw: Any) -> EvidenceRef:
    watermark = int(raw["last_seen_watermark"])
    first_seen = int(raw["first_seen_watermark"])
    return EvidenceRef(
        ref_id=str(raw["ref_id"]),
        tenant=str(raw["tenant"] or "default"),
        ref_type=str(raw["ref_type"]),
        preview=str(raw["preview"] or ""),
        metadata=_parse_metadata(raw["metadata"]),
        watermark=watermark,
        first_seen_watermark=first_seen,
        contains_secret=bool(raw["contains_secret"]),
    )


def get_ref(ref_id: str, *, tenant: str = "default") -> EvidenceRef | None:
    """Return the ref for ``(tenant, ref_id)``, or ``None`` if missing."""
    _ensure_table()
    cleaned = (ref_id or "").strip()
    if not cleaned:
        return None
    scoped = normalize_tenant(tenant)
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM evidence_refs WHERE tenant=? AND ref_id=?"),
            (scoped, cleaned),
        ).fetchone()
    return _row(row) if row else None


def list_refs(*, tenant: str = "default") -> list[EvidenceRef]:
    _ensure_table()
    scoped = normalize_tenant(tenant)
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT * FROM evidence_refs WHERE tenant=? ORDER BY last_seen_watermark, ref_id"
            ),
            (scoped,),
        ).fetchall()
    return [_row(row) for row in rows]


def ingest_ref(
    *,
    ref_id: str,
    preview: str = "",
    metadata: Mapping[str, Any] | None = None,
    ref_type: str = "manual_request",
    tenant: str = "default",
) -> EvidenceRef:
    """Store one ref after redaction. Watermark is the new ``change_log.id``."""
    _ensure_table()
    cleaned_id = (ref_id or "").strip()
    if not cleaned_id:
        raise EvidenceError("ref_id is required")
    scoped = normalize_tenant(tenant)
    cleaned_type = normalize_ref_type(ref_type)
    raw_preview = preview or ""
    raw_metadata = _parse_metadata(dict(metadata) if metadata is not None else {})
    stored_preview = redact_evidence_text(raw_preview)
    stored_metadata = redact_evidence_metadata(raw_metadata)
    if not isinstance(stored_metadata, dict):
        raise EvidenceError("metadata must be a mapping")
    contains_secret = stored_preview != raw_preview or stored_metadata != raw_metadata

    watermark = events.emit(
        EVIDENCE_EVENT_KIND,
        EVIDENCE_ENTITY_TYPE,
        cleaned_id,
        tenant=scoped,
        payload={"ref_id": cleaned_id, "ref_type": cleaned_type},
    )
    if watermark is None:
        raise EvidenceError("failed to emit change_log watermark for evidence ingest")

    existing = get_ref(cleaned_id, tenant=scoped)
    first_seen = existing.first_seen_watermark if existing is not None else watermark
    payload_json = json.dumps(stored_metadata, ensure_ascii=False, sort_keys=True)
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO evidence_refs (
                    tenant, ref_id, ref_type, preview, metadata,
                    contains_secret, first_seen_watermark, last_seen_watermark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant, ref_id) DO UPDATE SET
                    ref_type=excluded.ref_type,
                    preview=excluded.preview,
                    metadata=excluded.metadata,
                    contains_secret=excluded.contains_secret,
                    last_seen_watermark=excluded.last_seen_watermark
                """
            ),
            (
                scoped,
                cleaned_id,
                cleaned_type,
                stored_preview,
                payload_json,
                1 if contains_secret else 0,
                first_seen,
                watermark,
            ),
        )
    stored = get_ref(cleaned_id, tenant=scoped)
    if stored is None:
        raise EvidenceError(f"failed to persist evidence ref {cleaned_id!r}")
    return stored


def build_packet(
    ref_ids: Sequence[str],
    *,
    tenant: str = "default",
    max_chars: int = DEFAULT_MAX_PACKET_CHARS,
    max_refs: int = DEFAULT_MAX_PACKET_REFS,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    packet_id: str = "",
) -> EvidencePacket:
    """Assemble a bounded packet from existing tenant-scoped refs."""
    scoped = normalize_tenant(tenant)
    budget_chars = max(0, int(max_chars))
    cap_refs = max(0, int(max_refs))
    cap_snippet = max(0, int(snippet_chars))
    selected: list[EvidenceRef] = []
    snippets: list[EvidenceSnippet] = []
    omitted: list[str] = []
    missing: list[str] = []
    used = 0
    high_watermark = 0

    for raw_id in ref_ids:
        ref_id = str(raw_id or "").strip()
        if not ref_id:
            continue
        ref = get_ref(ref_id, tenant=scoped)
        if ref is None:
            missing.append(ref_id)
            continue
        if len(selected) >= cap_refs:
            omitted.append(ref_id)
            continue
        remaining = budget_chars - used
        if remaining <= 0:
            omitted.append(ref_id)
            continue
        take = min(cap_snippet, remaining, len(ref.preview))
        text = ref.preview[:take]
        truncation = "none" if take >= len(ref.preview) else "truncated"
        selected.append(ref)
        snippets.append(EvidenceSnippet(ref_id=ref.ref_id, text=text, truncation=truncation))
        used += len(text)
        if ref.watermark > high_watermark:
            high_watermark = ref.watermark

    if missing:
        status = "missing_refs"
    elif omitted:
        status = "bounded"
    else:
        status = "ok"
    return EvidencePacket(
        packet_id=(packet_id or "").strip() or uuid.uuid4().hex,
        tenant=scoped,
        watermark=high_watermark,
        refs=tuple(selected),
        snippets=tuple(snippets),
        budget=PacketBudget(
            max_chars=budget_chars,
            used_chars=used,
            omitted_refs=tuple(omitted),
        ),
        missing_refs=tuple(missing),
        redaction_status="redacted",
        build_status=status,
    )


def assess_evolution_claim(
    payload: Mapping[str, Any] | None,
    *,
    tenant: str = "default",
) -> Admissibility:
    """A claim is admissible only when every cited ref exists for ``tenant``."""
    scoped = normalize_tenant(tenant)
    checked = claim_ref_ids(payload)
    if not checked:
        return Admissibility(
            admissible=False,
            tenant=scoped,
            reason="not admissible: evolution claim has no evidence refs",
        )
    missing = tuple(ref_id for ref_id in checked if get_ref(ref_id, tenant=scoped) is None)
    if missing:
        listed = ", ".join(missing)
        return Admissibility(
            admissible=False,
            tenant=scoped,
            checked_refs=checked,
            missing_refs=missing,
            reason=f"not admissible: missing evidence refs: {listed}",
        )
    return Admissibility(
        admissible=True,
        tenant=scoped,
        checked_refs=checked,
        reason="",
    )
