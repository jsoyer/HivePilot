"""HP-97 unified PASS store — tool + memory + skill_evolution proposals.

Coworker PASS / tool-gateway pattern, rewritten in Python. Does not vendor TS.

One inbox. ``kind`` is the HP-94 discriminant (from ``tool_catalog.APPROVAL_KINDS``)::

    partition | tool | memory | skill_evolution

HP-61 already uses metadata ``kind`` as the *action token*
(e.g. ``pipeline_checkpoint``). PASS therefore stores the HP-94 kind in
column ``kind`` and, when composing ``match_auto``, partitions:

- HP-61 action ← ``action`` / ``token`` / a non-HP-94 ``kind``
- ``pass_kind`` ← HP-94 discriminant (never a change_class, never an action)

Statuses: PENDING | APPROVED | REJECTED | EDITED | EXPIRED.

``decide`` persists the status **before** any ``side_effect`` callback.
``submit(kind=skill_evolution)`` rejects a claim that is not HP-99
admissible (missing or foreign-tenant evidence refs). This module does
not apply memory writes, tool calls, skill promotions, or idempotent
side-effect tables (HP-100 / HP-101 / HP-105 / HP-109).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from hivepilot.evidence import assess_evolution_claim
from hivepilot.services import approval_rules_service, db, state_service
from hivepilot.services.approval_rules_service import (
    MECHANICAL,
    Auto,
    claimed_change_class,
    normalize_change_class,
)
from hivepilot.tool_catalog import (
    APPROVAL_KINDS,
    TOOL_APPROVAL_KIND,
    ToolCatalog,
    resolve,
)

SKILL_EVOLUTION_KIND = "skill_evolution"

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
STATUSES: tuple[str, ...] = ("PENDING", "APPROVED", "REJECTED", "EDITED", "EXPIRED")
PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EDITED = "EDITED"
EXPIRED = "EXPIRED"

DECISIONS: tuple[str, ...] = ("approve", "reject", "edit", "expire")

# Edit may change body text / args; it must not retarget the locked object.
FROZEN_TARGET_KEYS: frozenset[str] = frozenset({"path", "revision", "expected_revision"})

_DECISION_TO_STATUS: dict[str, str] = {
    "approve": APPROVED,
    "approved": APPROVED,
    "reject": REJECTED,
    "rejected": REJECTED,
    "deny": REJECTED,
    "denied": REJECTED,
    "edit": EDITED,
    "edited": EDITED,
    "expire": EXPIRED,
    "expired": EXPIRED,
}

TERMINAL: frozenset[str] = frozenset({APPROVED, REJECTED, EDITED, EXPIRED})

SideEffect = Callable[["PassProposal"], None]


class PassStoreError(ValueError):
    """Invalid PASS proposal, decision, or edit retarget."""


@dataclass(frozen=True)
class PassProposal:
    """One inbox row. ``kind`` is HP-94; ``action`` is the HP-61 token."""

    id: str
    kind: str
    status: str
    project: str
    task: str
    action: str
    change_class: str
    payload: dict[str, Any]
    tenant: str = "default"
    decided_at: str = ""
    decided_by: str = ""
    reason: str = ""
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "project": self.project,
            "task": self.task,
            "action": self.action,
            "change_class": self.change_class,
            "payload": dict(self.payload),
            "tenant": self.tenant,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def normalize_kind(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in APPROVAL_KINDS:
        raise PassStoreError(f"kind must be one of {sorted(APPROVAL_KINDS)}; got {raw!r}")
    return cleaned


def _normalize_decision(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in _DECISION_TO_STATUS:
        raise PassStoreError(f"decision must be one of {list(DECISIONS)}; got {raw!r}")
    return cleaned


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_proposals (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                change_class TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                tenant TEXT NOT NULL DEFAULT 'default',
                decided_at TIMESTAMP,
                decided_by TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
            """
        )


def _parse_payload(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise PassStoreError("payload must be a JSON object")
        return loaded
    raise PassStoreError("payload must be a mapping")


def _row(raw: Any) -> PassProposal:
    decided_at = raw["decided_at"]
    created_at = raw["created_at"]
    expires_at = raw["expires_at"]
    return PassProposal(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        status=str(raw["status"]),
        project=str(raw["project"] or ""),
        task=str(raw["task"] or ""),
        action=str(raw["action"] or ""),
        change_class=str(raw["change_class"] or ""),
        payload=_parse_payload(raw["payload"]),
        tenant=str(raw["tenant"] or "default"),
        decided_at="" if decided_at is None else str(decided_at),
        decided_by=str(raw["decided_by"] or ""),
        reason=str(raw["reason"] or ""),
        created_at="" if created_at is None else str(created_at),
        expires_at="" if expires_at is None else str(expires_at),
    )


def get(proposal_id: str) -> PassProposal | None:
    _ensure_table()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM pass_proposals WHERE id=?"),
            (proposal_id,),
        ).fetchone()
    return _row(row) if row else None


def inbox(
    *,
    kind: str | None = None,
    status: str | None = PENDING,
    tenant: str | None = None,
) -> list[PassProposal]:
    """One inbox. Filter by HP-94 ``kind`` and/or status (default PENDING)."""
    _ensure_table()
    clauses: list[str] = []
    params: list[Any] = []
    if kind is not None and kind != "":
        clauses.append("kind=?")
        params.append(normalize_kind(kind))
    if status is not None and status != "":
        cleaned = status.strip().upper()
        if cleaned not in STATUSES:
            raise PassStoreError(f"status must be one of {list(STATUSES)}")
        clauses.append("status=?")
        params.append(cleaned)
    if tenant is not None:
        clauses.append("tenant=?")
        params.append(tenant)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM pass_proposals{where} ORDER BY created_at, id"
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [_row(row) for row in rows]


def create_pending(
    *,
    kind: str,
    project: str = "",
    task: str = "",
    action: str = "",
    change_class: str = "",
    payload: Mapping[str, Any] | None = None,
    tenant: str = "default",
    expires_at: str = "",
    proposal_id: str = "",
) -> PassProposal:
    """Persist a PENDING proposal. Does not run a side-effect."""
    _ensure_table()
    cleaned_kind = normalize_kind(kind)
    body = _parse_payload(dict(payload) if payload is not None else {})
    action_token = (action or str(body.get("token") or body.get("action") or "")).strip()
    stored_class = normalize_change_class(change_class, persist=True)
    row_id = (proposal_id or "").strip() or uuid.uuid4().hex
    expiry = expires_at.strip() or None
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO pass_proposals (
                    id, kind, status, project, task, action, change_class,
                    payload, tenant, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                row_id,
                cleaned_kind,
                PENDING,
                (project or "").strip(),
                (task or "").strip(),
                action_token,
                stored_class,
                json.dumps(body, ensure_ascii=False, sort_keys=True),
                (tenant or "default").strip() or "default",
                expiry,
            ),
        )
    stored = get(row_id)
    if stored is None:
        raise PassStoreError(f"failed to persist proposal {row_id}")
    return stored


def _frozen_targets(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in FROZEN_TARGET_KEYS if key in payload}


def merge_edit(original: Mapping[str, Any], edited: Mapping[str, Any]) -> dict[str, Any]:
    """Merge an edit. Refuses any change to path / revision targets."""
    if not isinstance(edited, Mapping):
        raise PassStoreError("edited payload must be a mapping")
    merged = dict(original)
    original_targets = _frozen_targets(original)
    for key, value in edited.items():
        if key in FROZEN_TARGET_KEYS:
            if key in original_targets and value != original_targets[key]:
                raise PassStoreError("edit cannot retarget path/revision")
            if key not in original_targets:
                raise PassStoreError("edit cannot retarget path/revision")
            continue
        merged[key] = value
    return merged


def decide(
    proposal_id: str,
    decision: str,
    *,
    actor: str = "",
    reason: str = "",
    edited_payload: Mapping[str, Any] | None = None,
    side_effect: SideEffect | None = None,
) -> PassProposal:
    """Persist the decision, then optionally run ``side_effect``.

    The UPDATE commits before ``side_effect`` runs. A raising callback
    cannot roll back the status.
    """
    _ensure_table()
    token = _normalize_decision(decision)
    status = _DECISION_TO_STATUS[token]
    if token == "edit" and edited_payload is None:
        raise PassStoreError("edit requires edited_payload")
    if token != "edit" and edited_payload is not None:
        raise PassStoreError("edited_payload is only valid for edit")

    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM pass_proposals WHERE id=?"),
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise PassStoreError(f"proposal not found: {proposal_id}")
        current = _row(row)
        if current.status in TERMINAL:
            raise PassStoreError(f"proposal {proposal_id} is already {current.status}")
        payload = current.payload
        if token == "edit":
            payload = merge_edit(current.payload, edited_payload or {})
        conn.execute(
            db.ph(
                """
                UPDATE pass_proposals
                SET status=?, payload=?, decided_by=?, reason=?,
                    decided_at=CURRENT_TIMESTAMP
                WHERE id=? AND status=?
                """
            ),
            (
                status,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                (actor or "").strip(),
                (reason or "").strip(),
                proposal_id,
                PENDING,
            ),
        )

    stored = get(proposal_id)
    if stored is None or stored.status != status:
        raise PassStoreError(f"failed to persist decision for {proposal_id}")
    if side_effect is not None:
        side_effect(stored)
    return stored


def partition_hp61_metadata(pass_kind: str, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip HP-94 ``kind`` so HP-61 does not treat it as an action token."""
    meta = dict(metadata or {})
    raw_kind = meta.pop("kind", None)
    action = meta.get("action") or meta.get("token") or meta.get("step_name") or ""
    if raw_kind and str(raw_kind) not in APPROVAL_KINDS:
        action = action or raw_kind
    out = {key: value for key, value in meta.items() if key != "pass_kind"}
    out["pass_kind"] = pass_kind
    if action:
        out["action"] = str(action)
    return out


def match_auto(
    *,
    kind: str,
    project: str = "",
    task: str = "",
    metadata: dict[str, Any] | None = None,
    catalog: ToolCatalog | None = None,
    policy_overrides: Mapping[str, str] | None = None,
) -> Auto | None:
    """Compose tool-catalog policy + HP-61 rules + HP-86 mechanical gate.

    ``auto=approve`` only when every layer agrees and the class is
    mechanical. Non-mechanical (product_fork / security / destructive /
    unknown / contested) never auto-approves. Catalog ``deny`` wins.
    Catalog ``require_approval`` stays HITL (rules may still deny).
    """
    cleaned_kind = normalize_kind(kind)
    meta = dict(metadata or {})
    hp61_meta = partition_hp61_metadata(cleaned_kind, meta)
    claimed = claimed_change_class(meta)

    catalog_denied = False
    catalog_needs_approval = False
    if cleaned_kind == TOOL_APPROVAL_KIND:
        token = str(meta.get("token") or meta.get("action") or "")
        decision = resolve(
            token,
            project=project or None,
            change_class=str(meta.get("change_class") or ""),
            catalog=catalog,
            policy_overrides=policy_overrides,
        )
        catalog_denied = decision.denied
        catalog_needs_approval = decision.needs_approval

    if catalog_denied:
        return "deny"

    rules_auto = approval_rules_service.match_auto(project=project, task=task, metadata=hp61_meta)

    if catalog_needs_approval:
        return "deny" if rules_auto == "deny" else None

    if rules_auto == "approve" and claimed and claimed != MECHANICAL:
        return None
    return rules_auto


def submit(
    *,
    kind: str,
    project: str = "",
    task: str = "",
    action: str = "",
    change_class: str = "",
    payload: Mapping[str, Any] | None = None,
    tenant: str = "default",
    expires_at: str = "",
    metadata: Mapping[str, Any] | None = None,
    catalog: ToolCatalog | None = None,
    policy_overrides: Mapping[str, str] | None = None,
    side_effect: SideEffect | None = None,
) -> PassProposal:
    """Create PENDING, then auto decide when ``match_auto`` is conclusive.

    The pending row is always written first. Auto approve/deny persist
    before ``side_effect``.
    """
    proposal = create_pending(
        kind=kind,
        project=project,
        task=task,
        action=action,
        change_class=change_class,
        payload=payload,
        tenant=tenant,
        expires_at=expires_at,
    )
    if proposal.kind == SKILL_EVOLUTION_KIND:
        admission = assess_evolution_claim(proposal.payload, tenant=proposal.tenant)
        if not admission.admissible:
            return decide(
                proposal.id,
                "reject",
                actor="evidence",
                reason=admission.reason,
            )
    composed_meta = {
        "token": proposal.action or (proposal.payload.get("token") or ""),
        "action": proposal.action,
        "change_class": proposal.change_class or change_class,
        "kind": proposal.kind,
    }
    if metadata:
        composed_meta.update(dict(metadata))
        composed_meta["kind"] = proposal.kind
        if proposal.change_class:
            composed_meta.setdefault("change_class", proposal.change_class)
    auto = match_auto(
        kind=proposal.kind,
        project=proposal.project,
        task=proposal.task,
        metadata=composed_meta,
        catalog=catalog,
        policy_overrides=policy_overrides,
    )
    if auto == "deny":
        return decide(
            proposal.id,
            "reject",
            actor="auto",
            reason="match_auto deny",
            side_effect=side_effect,
        )
    if auto == "approve":
        return decide(
            proposal.id,
            "approve",
            actor="auto",
            reason="match_auto approve",
            side_effect=side_effect,
        )
    return proposal
