"""HP-101 memory proposals HITL — propose / approve / edit / reject.

Coworker memory HITL pattern, rewritten in Python. Does not vendor TS.

Every **model** write becomes a PASS proposal (``kind=memory``). Never
silent. No Always-allow. ``pending`` / ``reject`` leave the corpus
unchanged. ``edit`` then ``approve`` applies the **user** text.

Recall is zero-approval. User Pollen / vault writes go through
``user_write`` and mutate IsolatedJsonMemory / workspace text directly.

Apply runs only after ``decide`` persists APPROVED (HP-97). The reserved
``idempotency_key`` (HP-100 ``side_effects``) makes apply-after-approve
run at most once. Presenter parity (Pollen ↔ Telegram) is
``hivepilot.presenters`` (HP-102). This module does not implement
WhatsApp or HP-67.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from hivepilot.pass_store import (
    APPROVED,
    PENDING,
    REJECTED,
    PassProposal,
    PassStoreError,
    create_pending,
    decide,
    inbox,
    stage_edit,
)
from hivepilot.pass_store import (
    get as get_proposal,
)
from hivepilot.side_effects import (
    COMPLETED,
    bind,
    cached_result,
    complete,
    mint_key,
    reserve,
)
from hivepilot.side_effects import (
    get as get_effect,
)
from hivepilot.tool_catalog import APPROVAL_KINDS
from hivepilot.workspace_text import (
    IsolatedJsonMemory,
    MemoryEnvelope,
    MemoryPutResult,
    WorkspaceEditResult,
    WorkspaceSnapshot,
    apply_workspace_text_edit,
)

MEMORY_KIND = "memory"
JSON_MEMORY = "json_memory"
WORKSPACE_TEXT = "workspace_text"
TARGETS: tuple[str, ...] = (JSON_MEMORY, WORKSPACE_TEXT)

SOURCE_MODEL = "model"
SOURCE_USER = "user"
SOURCES: tuple[str, ...] = (SOURCE_MODEL, SOURCE_USER)

MEMORY_WRITE_TOKEN = "memory.write"
USER_ACTORS: frozenset[str] = frozenset({"user", "pollen", "vault"})

# Closed vocabulary. Always-allow is never a legal model-write policy.
ALWAYS_ALLOW_TOKENS: frozenset[str] = frozenset(
    {
        "always_allow",
        "always-allow",
        "alwaysallow",
        "always allow",
        "auto",
        "allow",
    }
)


class MemoryProposalError(ValueError):
    """Invalid memory proposal, decision, or source."""


@dataclass
class MemoryCorpus:
    """In-process corpus: isolated JSON memory + workspace documents.

    Pending PASS rows are not part of the corpus. Recall and user writes
    read/write these stores only.
    """

    memory: IsolatedJsonMemory = field(default_factory=IsolatedJsonMemory)
    documents: dict[str, WorkspaceSnapshot] = field(default_factory=dict)

    def workspace(self, path: str) -> WorkspaceSnapshot:
        return self.documents.get(path, WorkspaceSnapshot(text="", revision=0))


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying an approved (or direct user) write."""

    ok: bool
    target: str
    key: str
    revision: int
    code: str = ""
    reason: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "target": self.target,
            "key": self.key,
            "revision": self.revision,
            "code": self.code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MemoryDecision:
    """HITL outcome. ``applied`` is True only after a successful approve apply."""

    proposal: PassProposal
    applied: bool
    apply: ApplyResult | None = None
    idempotency_key: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.to_dict(),
            "applied": self.applied,
            "apply": None if self.apply is None else self.apply.to_dict(),
            "idempotency_key": self.idempotency_key,
            "cached": self.cached,
        }


def normalize_target(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in TARGETS:
        raise MemoryProposalError(f"target must be one of {list(TARGETS)}; got {raw!r}")
    return cleaned


def _normalize_source(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in SOURCES:
        raise MemoryProposalError(f"source must be one of {list(SOURCES)}; got {raw!r}")
    return cleaned


def _refuse_always_allow(policy: str, always_allow: bool) -> None:
    if always_allow:
        raise MemoryProposalError("model memory writes cannot Always-allow")
    token = (policy or "").strip().lower()
    if token in ALWAYS_ALLOW_TOKENS:
        raise MemoryProposalError("model memory writes cannot Always-allow")


def _expected_revision(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("expected_revision", payload.get("revision"))
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MemoryProposalError("expected_revision must be an int")
    return raw


def _body_value(payload: Mapping[str, Any]) -> Any:
    if "value" in payload:
        return payload["value"]
    if "text" in payload:
        return payload["text"]
    return None


def _workspace_new_text(payload: Mapping[str, Any]) -> str:
    if "new_text" in payload:
        return str(payload.get("new_text") or "")
    return str(payload.get("text") or "")


def _payload_key(payload: Mapping[str, Any], target: str) -> str:
    if target == JSON_MEMORY:
        return str(payload.get("key") or payload.get("path") or "")
    return str(payload.get("path") or "")


def _build_payload(
    *,
    target: str,
    key: str,
    path: str,
    expected_revision: int,
    value: Any,
    text: str,
    old_text: str,
    new_text: str,
    source: str,
    actor: str,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "target": target,
        "source": source,
        "expected_revision": expected_revision,
        "revision": expected_revision,
    }
    if target == JSON_MEMORY:
        stored_key = (key or path).strip()
        if not stored_key:
            raise MemoryProposalError("json_memory requires key")
        body["key"] = stored_key
        body["path"] = stored_key
        if value is not None:
            body["value"] = value
        elif text != "":
            body["value"] = text
            body["text"] = text
        else:
            raise MemoryProposalError("json_memory requires value or text")
    else:
        stored_path = (path or key).strip()
        if not stored_path:
            raise MemoryProposalError("workspace_text requires path")
        body["path"] = stored_path
        body["old_text"] = old_text
        body["new_text"] = new_text if new_text != "" or "new_text" in (extra or {}) else text
        body["text"] = body["new_text"]
    if actor:
        body["actor"] = actor
    if extra:
        for field_name, field_value in extra.items():
            if field_name in {"path", "revision", "expected_revision"}:
                continue
            body.setdefault(field_name, field_value)
    return body


def apply_payload(corpus: MemoryCorpus, payload: Mapping[str, Any]) -> ApplyResult:
    """Mutate the corpus from a decided (or user-direct) payload."""
    target = normalize_target(str(payload.get("target") or ""))
    key = _payload_key(payload, target)
    expected = _expected_revision(payload)
    if target == JSON_MEMORY:
        if not key:
            raise MemoryProposalError("json_memory requires key")
        put: MemoryPutResult = corpus.memory.put(
            key,
            _body_value(payload),
            expected_revision=expected,
        )
        return ApplyResult(
            ok=put.ok,
            target=target,
            key=key,
            revision=put.revision,
            code=put.code,
            reason=put.reason,
        )
    current = corpus.workspace(key)
    edit: WorkspaceEditResult = apply_workspace_text_edit(
        text=current.text,
        revision=current.revision,
        old_text=str(payload.get("old_text") or ""),
        new_text=_workspace_new_text(payload),
        expected_revision=expected,
    )
    if edit.ok:
        corpus.documents[key] = edit.snapshot
    return ApplyResult(
        ok=edit.ok,
        target=target,
        key=key,
        revision=edit.snapshot.revision,
        code=edit.code,
        reason=edit.reason,
    )


def recall(corpus: MemoryCorpus, key: str) -> MemoryEnvelope | None:
    """Read isolated JSON memory. No PASS row, no approval."""
    return corpus.memory.render(key)


def user_write(
    corpus: MemoryCorpus,
    *,
    target: str = JSON_MEMORY,
    key: str = "",
    path: str = "",
    expected_revision: int,
    value: Any = None,
    text: str = "",
    old_text: str = "",
    new_text: str = "",
    actor: str = "pollen",
) -> ApplyResult:
    """Direct user write (Pollen / vault). No proposal, no Always-allow gate.

    Model callers must use ``propose_model_write``.
    """
    cleaned_actor = (actor or "").strip().lower()
    if cleaned_actor not in USER_ACTORS:
        raise MemoryProposalError(
            f"user_write actor must be one of {sorted(USER_ACTORS)}; got {actor!r}"
        )
    payload = _build_payload(
        target=normalize_target(target),
        key=key,
        path=path,
        expected_revision=expected_revision,
        value=value,
        text=text,
        old_text=old_text,
        new_text=new_text,
        source=SOURCE_USER,
        actor=cleaned_actor,
        extra=None,
    )
    return apply_payload(corpus, payload)


def propose_model_write(
    *,
    target: str = JSON_MEMORY,
    key: str = "",
    path: str = "",
    expected_revision: int,
    value: Any = None,
    text: str = "",
    old_text: str = "",
    new_text: str = "",
    project: str = "",
    task: str = "",
    tenant: str = "default",
    policy: str = "",
    always_allow: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> PassProposal:
    """Persist a PENDING ``kind=memory`` proposal. Corpus is unchanged.

    Never calls ``submit`` / ``match_auto``. Always-allow is refused.
    """
    _refuse_always_allow(policy, always_allow)
    if MEMORY_KIND not in APPROVAL_KINDS:
        raise MemoryProposalError("memory is not an Approvals kind")
    payload = _build_payload(
        target=normalize_target(target),
        key=key,
        path=path,
        expected_revision=expected_revision,
        value=value,
        text=text,
        old_text=old_text,
        new_text=new_text,
        source=SOURCE_MODEL,
        actor="model",
        extra=extra,
    )
    idempotency_key = mint_key()
    payload["idempotency_key"] = idempotency_key
    reserve(idempotency_key, token=MEMORY_WRITE_TOKEN, volatile=False)
    proposal = create_pending(
        kind=MEMORY_KIND,
        project=project,
        task=task,
        action=MEMORY_WRITE_TOKEN,
        payload=payload,
        tenant=tenant,
    )
    bind(idempotency_key, proposal_id=proposal.id)
    return proposal


def pending_memory(*, tenant: str | None = None) -> list[PassProposal]:
    """PASS inbox filtered to ``kind=memory`` PENDING rows."""
    return inbox(kind=MEMORY_KIND, status=PENDING, tenant=tenant)


def _require_memory(proposal: PassProposal) -> None:
    if proposal.kind != MEMORY_KIND:
        raise MemoryProposalError(f"proposal {proposal.id} is kind={proposal.kind}, not memory")


def _idempotency_key(proposal: PassProposal) -> str:
    return str(proposal.payload.get("idempotency_key") or "")


def _apply_approved(corpus: MemoryCorpus, proposal: PassProposal) -> MemoryDecision:
    key = _idempotency_key(proposal)
    effect = get_effect(key) if key else None
    if effect is not None and effect.status == COMPLETED:
        cached = cached_result(key)
        apply = None
        if isinstance(cached, dict):
            apply = ApplyResult(
                ok=bool(cached.get("ok", True)),
                target=str(cached.get("target") or proposal.payload.get("target") or ""),
                key=str(cached.get("key") or ""),
                revision=int(cached.get("revision") or 0),
                code=str(cached.get("code") or ""),
                reason=str(cached.get("reason") or ""),
            )
        return MemoryDecision(
            proposal=proposal,
            applied=apply.ok if apply is not None else True,
            apply=apply,
            idempotency_key=key,
            cached=True,
        )
    applied = apply_payload(corpus, proposal.payload)
    if key:
        complete(key, applied.to_dict())
    return MemoryDecision(
        proposal=proposal,
        applied=applied.ok,
        apply=applied,
        idempotency_key=key,
        cached=False,
    )


def decide_memory(
    proposal_id: str,
    decision: str,
    *,
    corpus: MemoryCorpus,
    actor: str = "",
    reason: str = "",
    edited_payload: Mapping[str, Any] | None = None,
) -> MemoryDecision:
    """Approve / reject / edit a memory proposal.

    * ``reject`` — persist REJECTED; corpus unchanged.
    * ``edit`` — stage user text; status stays PENDING; corpus unchanged.
    * ``approve`` — persist APPROVED, then apply IsolatedJsonMemory /
      workspace text. Optional ``edited_payload`` is staged first so
      edit+approve applies the user text.
    """
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise PassStoreError(f"proposal not found: {proposal_id}")
    _require_memory(proposal)
    token = (decision or "").strip().lower()

    if token == "edit":
        if edited_payload is None:
            raise MemoryProposalError("edit requires edited_payload")
        staged = stage_edit(proposal_id, edited_payload, actor=actor)
        return MemoryDecision(
            proposal=staged,
            applied=False,
            idempotency_key=_idempotency_key(staged),
        )

    if token in {"reject", "rejected", "deny", "denied"}:
        stored = decide(proposal_id, "reject", actor=actor, reason=reason)
        return MemoryDecision(
            proposal=stored,
            applied=False,
            idempotency_key=_idempotency_key(stored),
        )

    if token not in {"approve", "approved"}:
        raise MemoryProposalError(f"decision must be approve, reject, or edit; got {decision!r}")

    if proposal.status == REJECTED:
        return MemoryDecision(
            proposal=proposal,
            applied=False,
            idempotency_key=_idempotency_key(proposal),
        )
    if proposal.status == PENDING:
        if edited_payload is not None:
            stage_edit(proposal_id, edited_payload, actor=actor)
        stored = decide(proposal_id, "approve", actor=actor, reason=reason)
    elif proposal.status == APPROVED:
        stored = proposal
    else:
        raise MemoryProposalError(f"proposal {proposal_id} is {proposal.status}, not approvable")
    return _apply_approved(corpus, stored)
